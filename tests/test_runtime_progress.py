"""Live progress on GET /documents/{workflow_id}/runtime."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pipeline.ocr import service as ocr_service
from pipeline.services import progress as live_progress
from tests.helpers import seed_document

WF = "wf-live"
#: Placeholder for "the current UTC time", stamped when the case runs so it is never stale.
NOW = "now"
FIFTEEN_MINUTES_AGO = (datetime.utcnow() - timedelta(minutes=15)).isoformat()


def _stamp(payload):
    if payload and payload.get("updated_at") == NOW:
        return {**payload, "updated_at": datetime.utcnow().isoformat()}
    return payload


def _describe(*, heartbeat=None, status="RUNNING", run_id="run-1"):
    activities = []
    if heartbeat is not None:
        activities = [SimpleNamespace(heartbeat_details=heartbeat, last_heartbeat_time=None)]
    return SimpleNamespace(
        run_id=run_id,
        status=SimpleNamespace(name=status),
        close_time=None,
        execution_time=None,
        pending_activities=activities,
        raw_description=None,
    )


@pytest.fixture(autouse=True)
def _reset_progress_cache():
    live_progress.clear_describe_cache()
    yield
    live_progress.clear_describe_cache()


@pytest.fixture
def running_handle(mock_temporal_client):
    handle = mock_temporal_client.get_workflow_handle.return_value
    handle.describe = AsyncMock(return_value=_describe())
    handle.query = AsyncMock(return_value={"stage": "ocr_processing"})
    return handle


def _seed(db, workflow_id=WF, *, stage="ocr_processing", page_count=10, chunk_count=0, **fields):
    seed_document(db, workflow_id, stage=stage, page_count=page_count, chunk_count=chunk_count, **fields)


def _ocr_segments(monkeypatch, page_count, **callbacks):
    """``ocr_pdf_in_segments`` over a fake ``page_count``-page PDF, 5 pages a segment.

    Returns the pages and the ``(start_idx, end_idx)`` of every provider call.
    """
    calls = []

    class _Provider:
        name = "fake"

        def process_pdf_range(self, pdf_path, start_idx, end_idx, *, log=None):
            calls.append((start_idx, end_idx))
            return [{"page_number": p, "original_markdown": f"page {p}"} for p in range(start_idx + 1, end_idx + 1)]

    monkeypatch.setattr(ocr_service, "get_ocr_provider", lambda config=None: _Provider())
    monkeypatch.setattr(ocr_service, "PdfReader", lambda path: SimpleNamespace(pages=[None] * page_count))
    return ocr_service.ocr_pdf_in_segments("doc.pdf", 5, lambda text: text, **callbacks), calls


@pytest.mark.parametrize(
    "payload, expected",
    [
        pytest.param(
            {"workflow_id": "wf", "stage": "chunking", "pages_processed": 3, "pages_total": 10, "chunks_emitted": 8},
            {"phase": "chunking", "done": 3, "total": 10, "unit": "pages"},
            id="chunking_stage_keys",
        ),
        pytest.param(
            {"workflow_id": "wf", "stage": "translation", "phase": "translation",
             "pages_completed": 5, "pages_total": 20},
            {"phase": "translation", "done": 5, "total": 20},
            id="translation_pages_completed",
        ),
    ],
)
def test_normalize_heartbeat_maps_stage_keys(payload, expected):
    out = live_progress.normalize_heartbeat(payload)
    assert {k: out[k] for k in expected} == expected


def _p(phase, done, total, updated_at=None):
    """A progress snapshot (SQLite or heartbeat) counted in pages."""
    return {"phase": phase, "done": done, "total": total, "unit": "pages", "updated_at": updated_at}


@pytest.mark.parametrize(
    "stage, sqlite, heartbeat, expected",
    [
        pytest.param("registered", _p(None, 0, None), _p("ocr", 0, 10), None, id="ignores_registered_stage"),
        pytest.param("ocr_review", _p(None, 10, 10), _p("ocr", 10, 10), None, id="ignores_review_stage"),
        pytest.param(
            "chunking", _p("chunking", 40, 196), _p("chunking", 0, 196, NOW), {"done": 40, "total": 196},
            id="zero_heartbeat_does_not_mask_sqlite",
        ),
        pytest.param(
            "ocr_processing", _p("ocr", 1, None, FIFTEEN_MINUTES_AGO), _p("ocr", 1, 9, FIFTEEN_MINUTES_AGO),
            {"stale": True}, id="marks_stale_heartbeat",
        ),
        pytest.param(
            "ocr_processing", _p("ocr", 4, None), _p("chunking", 99, 99), {"done": 4, "source": "sqlite", "total": None},
            id="ignores_heartbeat_from_other_phase",
        ),
        pytest.param(
            "translation_processing", _p("translation", 80, 100), _p("translation", 5, 20, NOW),
            {"done": 5, "total": 20}, id="translation_does_not_mix_historical_with_remaining_work",
        ),
        pytest.param(
            "ocr_processing", _p("ocr", 0, None), _p("ocr", 0, 5, NOW), {"done": 0, "total": 5, "source": "mixed"},
            id="ocr_zero_of_n_keeps_heartbeat_total",
        ),
        pytest.param(
            "ocr_processing", _p("ocr", 5, None), _p("ocr", 0, 8, NOW), {"done": 5, "total": 8},
            id="ocr_zero_heartbeat_does_not_hide_saved_pages",
        ),
        pytest.param(
            "translation_processing", _p("translation", 12, 20), _p("translation", 10, 20, NOW),
            {"done": 12, "total": 20}, id="same_scale_translation_keeps_max_done",
        ),
    ],
)
def test_assemble_progress(stage, sqlite, heartbeat, expected):
    out = live_progress.assemble_progress(stage=stage, sqlite=sqlite, heartbeat=_stamp(heartbeat))
    if expected is None:
        assert out is None
    else:
        assert {k: out[k] for k in expected} == expected


def test_runtime_progress_null_when_flag_off(test_client, db_connection, running_handle, monkeypatch):
    monkeypatch.delenv("LIVE_PROGRESS_UI_ENABLED", raising=False)
    _seed(db_connection, page_count=7)
    resp = test_client.get(f"/documents/{WF}/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sqlite_stage"] == "ocr_processing"
    assert body["progress"] is None


def test_runtime_progress_null_when_temporal_down(test_client, db_connection, monkeypatch):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    _seed(db_connection, page_count=7)
    monkeypatch.setattr(
        "pipeline.services.workflow_runtime.temporal_client.get_client_or_none", AsyncMock(return_value=None)
    )
    resp = test_client.get(f"/documents/{WF}/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sqlite_stage"] == "ocr_processing"
    assert body["temporal_connected"] is False
    assert body["progress"] is None


@pytest.mark.parametrize(
    "seed, heartbeat, expected",
    [
        pytest.param(
            {"stage": "ocr_review", "page_count": 7},
            {"phase": "ocr", "done": 7, "total": 7, "unit": "pages"},
            None,
            id="null_on_review_stage",
        ),
        pytest.param(
            {"page_count": 10},
            {"workflow_id": WF, "phase": "ocr", "done": 120, "total": 500, "unit": "pages", "updated_at": NOW},
            {"phase": "ocr", "done": 120, "total": 500, "unit": "pages", "source": "mixed", "stale": False},
            id="from_heartbeat_and_sqlite",
        ),
        pytest.param(
            {"stage": "ingesting", "page_count": 4, "chunk_count": 200},
            {"stage": "ingest", "batch": 2, "rows_seen": 40, "rows_total": 200},
            {"phase": "ingest", "done": 40, "total": 200, "unit": "chunks", "stale": False},
            id="maps_ingest_row_heartbeats",
        ),
        pytest.param(
            {"page_count": 18},
            None,
            {"phase": "ocr", "done": 18, "total": None, "source": "sqlite"},
            id="sqlite_only_when_heartbeat_missing",
        ),
    ],
)
def test_runtime_progress(test_client, db_connection, running_handle, monkeypatch, seed, heartbeat, expected):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    _seed(db_connection, **seed)
    if heartbeat is not None:
        running_handle.describe = AsyncMock(return_value=_describe(heartbeat=_stamp(heartbeat)))
    resp = test_client.get(f"/documents/{WF}/runtime")
    assert resp.status_code == 200
    progress = resp.json()["progress"]
    if expected is None:
        assert progress is None
    else:
        assert {k: progress[k] for k in expected} == expected


def test_runtime_uses_running_job_temporal_workflow_id(
    test_client, db_connection, mock_temporal_client, running_handle, monkeypatch
):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    _seed(db_connection)
    db_connection.create_document_job(
        workflow_id=WF, job_type="ocr_retry", temporal_workflow_id="retry-temporal-id", status="running"
    )
    mock_temporal_client.get_workflow_handle.reset_mock()
    resp = test_client.get(f"/documents/{WF}/runtime")
    assert resp.status_code == 200
    mock_temporal_client.get_workflow_handle.assert_called_with("retry-temporal-id")
    assert resp.json()["temporal"]["workflow_id"] == "retry-temporal-id"


def test_runtime_describe_is_ttl_coalesced(test_client, db_connection, running_handle, monkeypatch):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    _seed(db_connection, page_count=2)
    running_handle.describe.reset_mock()
    assert test_client.get(f"/documents/{WF}/runtime").status_code == 200
    assert test_client.get(f"/documents/{WF}/runtime").status_code == 200
    assert running_handle.describe.await_count == 1


def test_list_documents_does_not_describe_temporal(test_client, db_connection, mock_temporal_client, monkeypatch):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    _seed(db_connection, "wf-list-a", page_count=3)
    _seed(db_connection, "wf-list-b", page_count=4)
    mock_temporal_client.get_workflow_handle.reset_mock()
    resp = test_client.get("/documents")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert {row["workflow_id"] for row in items} >= {"wf-list-a", "wf-list-b"}
    assert all("progress" not in row for row in items)
    mock_temporal_client.get_workflow_handle.assert_not_called()


def test_runtime_unknown_document_is_404(test_client, monkeypatch):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    assert test_client.get("/documents/does-not-exist/runtime").status_code == 404


def test_count_translated_pages_does_not_require_full_page_load(db_connection):
    _seed(db_connection, stage="translation_processing", page_count=3, pages=[
        {"page_number": 1, "original_markdown": "a", "translated_markdown": "A"},
        {"page_number": 2, "original_markdown": "b"},
        {"page_number": 3, "original_markdown": "c", "edited_translation": "C"},
    ])
    assert db_connection.count_translated_pages(WF) == 2


def test_extract_pending_heartbeat_from_list():
    description = _describe(heartbeat={"phase": "ocr", "pages_saved": 5, "total_pages": 9})
    out = asyncio.run(live_progress.extract_pending_heartbeat(description))
    assert out["done"] == 5
    assert out["total"] == 9


def test_extract_decodes_temporal_payloads():
    from temporalio.api.common.v1 import Payloads
    from temporalio.converter import DataConverter

    converter = DataConverter.default
    payloads = asyncio.run(converter.encode([{"phase": "ocr", "done": 3, "total": 9, "unit": "pages"}]))
    description = SimpleNamespace(
        pending_activities=None,
        raw_description=SimpleNamespace(
            pending_activities=[SimpleNamespace(heartbeat_details=Payloads(payloads=payloads))]
        ),
        _context_free_data_converter=converter,
    )
    out = asyncio.run(live_progress.extract_pending_heartbeat(description))
    assert out["phase"] == "ocr"
    assert out["done"] == 3
    assert out["total"] == 9


def test_extract_skips_empty_protobuf_heartbeat_details():
    from temporalio.api.common.v1 import Payloads

    activity = SimpleNamespace(heartbeat_details=Payloads(), last_heartbeat_time=None, HasField=lambda name: False)
    description = SimpleNamespace(pending_activities=[activity], raw_description=None)
    assert asyncio.run(live_progress.extract_pending_heartbeat(description)) is None


def test_extract_heartbeat_aware_last_heartbeat_time_does_not_raise():
    ts = datetime.now(timezone.utc)
    description = _describe(heartbeat={"phase": "ocr", "done": 2, "total": 9})
    description.pending_activities[0].last_heartbeat_time = ts
    out = asyncio.run(live_progress.extract_pending_heartbeat(description))
    assert out["done"] == 2
    assert out["updated_at"]
    assert live_progress._is_stale(out["updated_at"], now=ts) is False
    assembled = live_progress.assemble_progress(
        stage="ocr_processing", sqlite={"phase": "ocr", "done": 2, "total": None, "unit": "pages"}, heartbeat=out
    )
    assert assembled["stale"] is False


def test_runtime_keeps_chunking_progress_when_live_flag_off(test_client, db_connection, running_handle, monkeypatch):
    monkeypatch.delenv("LIVE_PROGRESS_UI_ENABLED", raising=False)
    _seed(db_connection, stage="chunking", page_count=2)
    db_connection.create_document_job(
        workflow_id=WF, job_type="chunking", temporal_workflow_id=WF, status="running", current_stage="chunking",
        config={"chunking_progress": {
            "status": "running", "pages_processed": 1, "pages_total": 2, "chunks_emitted": 4, "percent": 50.0,
        }},
    )
    resp = test_client.get(f"/documents/{WF}/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body["progress"] is None
    assert body["chunking_progress"]["pages_processed"] == 1
    assert body["chunking_progress"]["pages_total"] == 2
    assert body["chunking_progress"]["chunks_emitted"] == 4


def test_ocr_segment_start_does_not_write_sqlite(db_connection, monkeypatch):
    """Start-of-segment ticks are heartbeats only; SQLite still grows only on persist."""
    _seed(db_connection, page_count=0)
    starts = []

    def on_start(start_idx, end_idx, total_pages):
        in_sqlite = [row["page_number"] for row in db_connection.get_pages(WF)]
        starts.append({"total": total_pages, "sqlite_pages": in_sqlite})

    pages, _ = _ocr_segments(
        monkeypatch, 8, on_segment_start=on_start,
        on_segment_complete=lambda segment, total: db_connection.save_pages(WF, segment),
    )

    assert len(starts) == 2
    assert starts[0]["sqlite_pages"] == []
    assert starts[0]["total"] == 8
    assert starts[1]["sqlite_pages"] == [1, 2, 3, 4, 5]
    stored = sorted(row["page_number"] for row in db_connection.get_pages(WF))
    assert stored == [1, 2, 3, 4, 5, 6, 7, 8]
    assert [row["page_number"] for row in pages] == stored
    assert all(row["original_markdown"] == f"page {row['page_number']}" for row in db_connection.get_pages(WF))


def test_ocr_skipped_segment_does_not_call_start(monkeypatch):
    starts = []
    pages, _ = _ocr_segments(
        monkeypatch, 8, on_segment_start=lambda *args: starts.append(args), completed_page_numbers={1, 2, 3, 4, 5}
    )
    assert starts == [(5, 8, 8)]
    assert [row["page_number"] for row in pages] == [6, 7, 8]


def test_ocr_pdf_in_segments_without_start_callback_still_persists(monkeypatch):
    completed = []
    pages, _ = _ocr_segments(
        monkeypatch, 1, on_segment_complete=lambda segment, total: completed.append((len(segment), total))
    )
    assert [row["page_number"] for row in pages] == [1]
    assert completed == [(1, 1)]


def test_ocr_zero_page_pdf_does_not_start_or_persist(monkeypatch):
    starts, completed = [], []
    pages, provider_calls = _ocr_segments(
        monkeypatch, 0,
        on_segment_start=lambda *_: starts.append(True), on_segment_complete=lambda *_: completed.append(True),
    )
    assert pages == []
    assert starts == []
    assert completed == []
    assert provider_calls == [], "empty PDFs must not call the OCR provider"


@pytest.mark.asyncio
async def test_describe_cache_expires_after_one_second(monkeypatch):
    handle = AsyncMock()
    handle.describe = AsyncMock(side_effect=["first", "second"])
    client = SimpleNamespace(get_workflow_handle=lambda _id: handle)
    clock = {"t": 10.0}
    monkeypatch.setattr(live_progress.time, "monotonic", lambda: clock["t"])

    first = await live_progress.describe_workflow_cached(client, "wf-ttl")
    clock["t"] = 10.9
    cached = await live_progress.describe_workflow_cached(client, "wf-ttl")
    clock["t"] = 11.0
    refreshed = await live_progress.describe_workflow_cached(client, "wf-ttl")

    assert first == cached == "first"
    assert refreshed == "second"
    assert handle.describe.await_count == 2
