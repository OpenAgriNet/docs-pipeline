"""Live progress on GET /documents/{workflow_id}/runtime."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pipeline.services import progress as live_progress


def _run(coro):
    return asyncio.run(coro)


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
def _reset_progress_cache(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    from pipeline.services import progress_cache

    live_progress.clear_describe_cache()
    progress_cache.reset_for_tests()
    yield
    live_progress.clear_describe_cache()
    progress_cache.reset_for_tests()


@pytest.fixture
def running_handle(mock_temporal_client):
    handle = mock_temporal_client.get_workflow_handle.return_value
    handle.describe = AsyncMock(return_value=_describe())
    handle.query = AsyncMock(return_value={"stage": "ocr_processing"})
    return handle


def _seed_doc(db, workflow_id, *, stage="ocr_processing", page_count=10, chunk_count=0):
    db.upsert_document(
        workflow_id=workflow_id,
        document_id=f"doc-{workflow_id}",
        filename=f"{workflow_id}.pdf",
        filepath=f"/tmp/{workflow_id}.pdf",
        stage=stage,
        page_count=page_count,
        chunk_count=chunk_count,
    )


@pytest.mark.unit
def test_normalize_heartbeat_maps_ocr_legacy_keys():
    out = live_progress.normalize_heartbeat(
        {"workflow_id": "wf", "pages_saved": 12, "total_pages": 50, "phase": "ocr"}
    )
    assert out == {
        "phase": "ocr",
        "done": 12,
        "total": 50,
        "unit": "pages",
        "updated_at": None,
    }


@pytest.mark.unit
def test_normalize_heartbeat_maps_chunking_stage_keys():
    out = live_progress.normalize_heartbeat(
        {
            "workflow_id": "wf",
            "stage": "chunking",
            "pages_processed": 3,
            "pages_total": 10,
            "chunks_emitted": 8,
        }
    )
    assert out["phase"] == "chunking"
    assert out["done"] == 3
    assert out["total"] == 10
    assert out["unit"] == "pages"


@pytest.mark.unit
def test_assemble_progress_ignores_registered_stage():
    assert (
        live_progress.assemble_progress(
            stage="registered",
            sqlite={"done": 0, "total": None, "unit": "pages"},
            heartbeat={"phase": "ocr", "done": 0, "total": 10, "unit": "pages"},
        )
        is None
    )


@pytest.mark.unit
def test_assemble_progress_ignores_review_stage():
    assert (
        live_progress.assemble_progress(
            stage="ocr_review",
            sqlite={"done": 10, "total": 10, "unit": "pages"},
            heartbeat={"phase": "ocr", "done": 10, "total": 10, "unit": "pages"},
        )
        is None
    )


@pytest.mark.unit
def test_assemble_progress_prefers_heartbeat_total_and_max_done():
    out = live_progress.assemble_progress(
        stage="ocr_processing",
        sqlite={"phase": "ocr", "done": 10, "total": None, "unit": "pages", "updated_at": None},
        heartbeat={
            "phase": "ocr",
            "done": 120,
            "total": 500,
            "unit": "pages",
            "updated_at": datetime.utcnow().isoformat(),
        },
    )
    assert out["done"] == 120
    assert out["total"] == 500
    assert out["source"] == "mixed"
    assert out["stale"] is False


@pytest.mark.unit
def test_assemble_progress_does_not_let_zero_heartbeat_mask_sqlite():
    out = live_progress.assemble_progress(
        stage="chunking",
        sqlite={"phase": "chunking", "done": 40, "total": 196, "unit": "pages", "updated_at": None},
        heartbeat={
            "phase": "chunking",
            "done": 0,
            "total": 196,
            "unit": "pages",
            "updated_at": datetime.utcnow().isoformat(),
        },
    )
    assert out["done"] == 40
    assert out["total"] == 196


@pytest.mark.unit
def test_progress_cache_roundtrip():
    from pipeline.services import progress_cache

    progress_cache.put("wf-a", {"phase": "ocr", "done": 2, "total": 9, "unit": "pages"})
    assert progress_cache.get("wf-a")["done"] == 2
    progress_cache.clear("wf-a")
    assert progress_cache.get("wf-a") is None


@pytest.mark.unit
def test_first_heartbeat_dict_accepts_bare_dict():
    assert live_progress._first_heartbeat_dict({"phase": "ocr", "done": 2, "total": 9}) == {
        "phase": "ocr",
        "done": 2,
        "total": 9,
    }
    assert live_progress._first_heartbeat_dict([{"phase": "ocr", "done": 2}])["done"] == 2
    assert live_progress._first_heartbeat_dict(None) is None


@pytest.mark.unit
def test_assemble_progress_marks_stale_heartbeat():
    old = (datetime.utcnow() - timedelta(minutes=15)).isoformat()
    out = live_progress.assemble_progress(
        stage="ocr_processing",
        sqlite={"phase": "ocr", "done": 1, "total": None, "unit": "pages", "updated_at": old},
        heartbeat={"phase": "ocr", "done": 1, "total": 9, "unit": "pages", "updated_at": old},
    )
    assert out["stale"] is True


@pytest.mark.unit
def test_assemble_progress_ignores_heartbeat_from_other_phase():
    out = live_progress.assemble_progress(
        stage="ocr_processing",
        sqlite={"phase": "ocr", "done": 4, "total": None, "unit": "pages", "updated_at": None},
        heartbeat={"phase": "chunking", "done": 99, "total": 99, "unit": "pages"},
    )
    assert out["done"] == 4
    assert out["source"] == "sqlite"
    assert out["total"] is None


@pytest.mark.api
def test_runtime_progress_null_when_flag_off(
    test_client, db_connection, running_handle, monkeypatch
):
    monkeypatch.delenv("LIVE_PROGRESS_UI_ENABLED", raising=False)
    _seed_doc(db_connection, "wf-flag-off", page_count=7)
    resp = test_client.get("/documents/wf-flag-off/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sqlite_stage"] == "ocr_processing"
    assert body["progress"] is None


@pytest.mark.api
def test_runtime_progress_null_when_temporal_down(test_client, db_connection, monkeypatch):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    _seed_doc(db_connection, "wf-no-temporal", page_count=7)
    monkeypatch.setattr(
        "pipeline.services.workflow_runtime.temporal_client.get_client_or_none",
        AsyncMock(return_value=None),
    )
    resp = test_client.get("/documents/wf-no-temporal/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sqlite_stage"] == "ocr_processing"
    assert body["temporal_connected"] is False
    assert body["progress"]["phase"] == "ocr"
    assert body["progress"]["done"] == 7
    assert body["progress"]["source"] == "sqlite"


@pytest.mark.api
def test_runtime_progress_null_on_review_stage(
    test_client, db_connection, running_handle, monkeypatch
):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    _seed_doc(db_connection, "wf-review", stage="ocr_review", page_count=7)
    running_handle.describe = AsyncMock(
        return_value=_describe(
            heartbeat={"phase": "ocr", "done": 7, "total": 7, "unit": "pages"}
        )
    )
    resp = test_client.get("/documents/wf-review/runtime")
    assert resp.status_code == 200
    assert resp.json()["progress"] is None


@pytest.mark.api
def test_runtime_progress_uses_cache_when_present(
    test_client, db_connection, running_handle, monkeypatch
):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    from pipeline.services import progress_cache

    _seed_doc(db_connection, "wf-ocr-job", stage="ocr_processing", page_count=4)
    progress_cache.put(
        "wf-ocr-job",
        {
            "phase": "ocr",
            "done": 4,
            "total": 20,
            "unit": "pages",
            "updated_at": datetime.utcnow().isoformat(),
        },
    )
    resp = test_client.get("/documents/wf-ocr-job/runtime")
    assert resp.status_code == 200
    progress = resp.json()["progress"]
    assert progress["phase"] == "ocr"
    assert progress["done"] == 4
    assert progress["total"] == 20
    assert progress["source"] == "mixed"


@pytest.mark.api
def test_runtime_progress_uses_translation_cache(
    test_client, db_connection, running_handle, monkeypatch
):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    from pipeline.services import progress_cache

    _seed_doc(db_connection, "wf-tr-job", stage="translation_processing", page_count=196)
    progress_cache.put(
        "wf-tr-job",
        {
            "phase": "translation",
            "done": 37,
            "total": 196,
            "unit": "pages",
            "updated_at": datetime.utcnow().isoformat(),
        },
    )
    resp = test_client.get("/documents/wf-tr-job/runtime")
    assert resp.status_code == 200
    progress = resp.json()["progress"]
    assert progress["phase"] == "translation"
    assert progress["done"] == 37
    assert progress["total"] == 196
    assert progress["source"] == "mixed"


@pytest.mark.api
def test_runtime_progress_from_heartbeat_and_sqlite(
    test_client, db_connection, running_handle, monkeypatch
):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    from pipeline.services import progress_cache

    _seed_doc(db_connection, "wf-ocr-live", page_count=10)
    progress_cache.put(
        "wf-ocr-live",
        {
            "phase": "ocr",
            "done": 120,
            "total": 500,
            "unit": "pages",
            "updated_at": datetime.utcnow().isoformat(),
        },
    )
    resp = test_client.get("/documents/wf-ocr-live/runtime")
    assert resp.status_code == 200
    progress = resp.json()["progress"]
    assert progress["phase"] == "ocr"
    assert progress["done"] == 120
    assert progress["total"] == 500
    assert progress["unit"] == "pages"
    assert progress["source"] == "mixed"
    assert progress["stale"] is False


@pytest.mark.api
def test_runtime_progress_maps_ingest_row_heartbeats(
    test_client, db_connection, running_handle, monkeypatch
):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    from pipeline.services import progress_cache

    _seed_doc(db_connection, "wf-ingest-live", stage="ingesting", page_count=4, chunk_count=200)
    progress_cache.put(
        "wf-ingest-live",
        {
            "phase": "ingest",
            "done": 40,
            "total": 200,
            "unit": "chunks",
        },
    )
    resp = test_client.get("/documents/wf-ingest-live/runtime")
    assert resp.status_code == 200
    progress = resp.json()["progress"]
    assert progress["phase"] == "ingest"
    assert progress["done"] == 40
    assert progress["total"] == 200
    assert progress["unit"] == "chunks"
    assert progress["stale"] is False


@pytest.mark.api
def test_runtime_progress_sqlite_only_when_heartbeat_missing(
    test_client, db_connection, running_handle, monkeypatch
):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    _seed_doc(db_connection, "wf-ocr-sqlite", page_count=18)
    resp = test_client.get("/documents/wf-ocr-sqlite/runtime")
    assert resp.status_code == 200
    progress = resp.json()["progress"]
    assert progress["phase"] == "ocr"
    assert progress["done"] == 18
    assert progress["total"] is None
    assert progress["source"] == "sqlite"


@pytest.mark.api
def test_runtime_uses_running_job_temporal_workflow_id(
    test_client, db_connection, mock_temporal_client, running_handle, monkeypatch
):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    _seed_doc(db_connection, "wf-retry")
    db_connection.create_document_job(
        workflow_id="wf-retry",
        job_type="ocr_retry",
        temporal_workflow_id="retry-temporal-id",
        status="running",
    )
    mock_temporal_client.get_workflow_handle.reset_mock()
    running_handle.describe = AsyncMock(return_value=_describe())
    resp = test_client.get("/documents/wf-retry/runtime")
    assert resp.status_code == 200
    mock_temporal_client.get_workflow_handle.assert_called_with("retry-temporal-id")
    assert resp.json()["temporal"]["workflow_id"] == "retry-temporal-id"


@pytest.mark.api
def test_runtime_describe_is_ttl_coalesced(
    test_client, db_connection, running_handle, monkeypatch
):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    _seed_doc(db_connection, "wf-ttl", page_count=2)
    running_handle.describe.reset_mock()
    first = test_client.get("/documents/wf-ttl/runtime")
    second = test_client.get("/documents/wf-ttl/runtime")
    assert first.status_code == 200
    assert second.status_code == 200
    assert running_handle.describe.await_count == 1


@pytest.mark.api
def test_list_documents_does_not_describe_temporal(
    test_client, db_connection, mock_temporal_client, monkeypatch
):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    _seed_doc(db_connection, "wf-list-a", page_count=3)
    _seed_doc(db_connection, "wf-list-b", page_count=4)
    mock_temporal_client.get_workflow_handle.reset_mock()
    resp = test_client.get("/documents")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert {row["workflow_id"] for row in items} >= {"wf-list-a", "wf-list-b"}
    assert all("progress" not in row for row in items)
    mock_temporal_client.get_workflow_handle.assert_not_called()


@pytest.mark.api
def test_runtime_unknown_document_is_404(test_client, monkeypatch):
    monkeypatch.setenv("LIVE_PROGRESS_UI_ENABLED", "true")
    resp = test_client.get("/documents/does-not-exist/runtime")
    assert resp.status_code == 404


@pytest.mark.db
def test_count_translated_pages_does_not_require_full_page_load(db_connection):
    _seed_doc(db_connection, "wf-tr", stage="translation_processing", page_count=3)
    db_connection.save_pages(
        "wf-tr",
        [
            {"page_number": 1, "original_markdown": "a", "translated_markdown": "A"},
            {"page_number": 2, "original_markdown": "b"},
            {"page_number": 3, "original_markdown": "c", "edited_translation": "C"},
        ],
    )
    assert db_connection.count_translated_pages("wf-tr") == 2


@pytest.mark.unit
def test_extract_pending_heartbeat_from_list():
    description = _describe(
        heartbeat={"phase": "ocr", "pages_saved": 5, "total_pages": 9}
    )
    out = _run(live_progress.extract_pending_heartbeat(description))
    assert out["done"] == 5
    assert out["total"] == 9


@pytest.mark.unit
def test_extract_decodes_temporal_payloads():
    from temporalio.api.common.v1 import Payloads
    from temporalio.converter import DataConverter

    converter = DataConverter.default
    payloads = _run(converter.encode([{"phase": "ocr", "done": 3, "total": 9, "unit": "pages"}]))
    description = SimpleNamespace(
        pending_activities=None,
        raw_description=SimpleNamespace(
            pending_activities=[SimpleNamespace(heartbeat_details=Payloads(payloads=payloads))]
        ),
        _context_free_data_converter=converter,
    )
    out = _run(live_progress.extract_pending_heartbeat(description))
    assert out["phase"] == "ocr"
    assert out["done"] == 3
    assert out["total"] == 9


@pytest.mark.unit
def test_is_stale_accepts_timezone_aware_utc_iso():
    now = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)
    assert live_progress._is_stale(now.isoformat(), now=now) is False
    old = now - timedelta(minutes=15)
    assert live_progress._is_stale(old.isoformat(), now=now) is True


@pytest.mark.unit
def test_extract_heartbeat_aware_last_heartbeat_time_does_not_raise():
    ts = datetime.now(timezone.utc)
    description = SimpleNamespace(
        run_id="run-1",
        status=SimpleNamespace(name="RUNNING"),
        close_time=None,
        execution_time=None,
        pending_activities=[
            SimpleNamespace(
                heartbeat_details={"phase": "ocr", "done": 2, "total": 9},
                last_heartbeat_time=ts,
            )
        ],
        raw_description=None,
    )
    out = _run(live_progress.extract_pending_heartbeat(description))
    assert out["done"] == 2
    assert out["updated_at"]
    assert live_progress._is_stale(out["updated_at"], now=ts) is False
    assembled = live_progress.assemble_progress(
        stage="ocr_processing",
        sqlite={"phase": "ocr", "done": 2, "total": None, "unit": "pages"},
        heartbeat=out,
    )
    assert assembled["stale"] is False


@pytest.mark.unit
def test_normalize_heartbeat_maps_ingest_row_fields():
    out = live_progress.normalize_heartbeat(
        {
            "stage": "ingest",
            "batch": 2,
            "rows_seen": 40,
            "rows_total": 200,
        }
    )
    assert out["phase"] == "ingest"
    assert out["done"] == 40
    assert out["total"] == 200
    assert out["unit"] == "chunks"


@pytest.mark.unit
def test_normalize_heartbeat_maps_translation_pages_completed():
    out = live_progress.normalize_heartbeat(
        {
            "workflow_id": "wf",
            "stage": "translation",
            "phase": "translation",
            "pages_completed": 5,
            "pages_total": 20,
        }
    )
    assert out["phase"] == "translation"
    assert out["done"] == 5
    assert out["total"] == 20


@pytest.mark.unit
def test_assemble_progress_translation_does_not_mix_historical_with_remaining_work():
    out = live_progress.assemble_progress(
        stage="translation_processing",
        sqlite={
            "phase": "translation",
            "done": 80,
            "total": 100,
            "unit": "pages",
            "updated_at": None,
        },
        heartbeat={
            "phase": "translation",
            "done": 5,
            "total": 20,
            "unit": "pages",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert out["done"] == 5
    assert out["total"] == 20


@pytest.mark.api
def test_runtime_keeps_chunking_progress_when_live_flag_off(
    test_client, db_connection, running_handle, monkeypatch
):
    monkeypatch.delenv("LIVE_PROGRESS_UI_ENABLED", raising=False)
    _seed_doc(db_connection, "wf-chunk-flag-off", stage="chunking", page_count=2)
    db_connection.create_document_job(
        workflow_id="wf-chunk-flag-off",
        job_type="chunking",
        temporal_workflow_id="wf-chunk-flag-off",
        status="running",
        current_stage="chunking",
        config={
            "chunking_progress": {
                "status": "running",
                "pages_processed": 1,
                "pages_total": 2,
                "chunks_emitted": 4,
                "percent": 50.0,
            }
        },
    )
    resp = test_client.get("/documents/wf-chunk-flag-off/runtime")
    assert resp.status_code == 200
    body = resp.json()
    assert body["progress"] is None
    assert body["chunking_progress"]["pages_processed"] == 1
    assert body["chunking_progress"]["pages_total"] == 2
    assert body["chunking_progress"]["chunks_emitted"] == 4


@pytest.mark.unit
def test_extract_pending_heartbeat_skips_mock_iterables():
    from unittest.mock import MagicMock

    out = _run(live_progress.extract_pending_heartbeat(MagicMock()))
    assert out is None
