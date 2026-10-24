"""OCR resume vs force redo (#123)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest
from fastapi import HTTPException

import pipeline.temporal.document_tasks as activities
from pipeline.routers import documents_actions as action_routes
from pipeline.services.documents import list_available_actions
from tests.helpers import seed_document

pytestmark = pytest.mark.unit

WF = "wf-ocr"
OPERATOR = SimpleNamespace(user_id="operator-1")
FORCE_REBUILD = {"reindex_required": 1, "reindex_reason": "force_ocr_requested"}


def _actions(stage, job=None, **doc):
    return set(list_available_actions({"stage": stage, "is_disabled": False, **doc}, current_job=job))


@pytest.fixture
def routes(monkeypatch):
    """The retry-OCR and reingest routes on a stub document, an autospec ``db`` and mock Temporal starts.

    ``routes.doc`` is the served row; ``update_document_fields`` writes into it. The autospec keeps
    the real names and signatures. The Temporal starts are children of ``routes.db``, so
    ``mock_calls`` orders them with the db calls.
    """
    db = create_autospec(action_routes.db)
    doc = {"document_id": "doc-1", "filename": "doc.pdf", "filepath": "/tmp/doc.pdf", "instance": "tenant-a"}
    db.create_document_job.return_value = 123
    db.list_document_index_status.return_value = []
    db.resolve_ingest_index_name.return_value = "tenant-a-physical-index"
    db.get_chunks.return_value = [{"chunk_number": 1}]
    db.update_document_fields.side_effect = lambda _wf, **fields: doc.update(fields)
    db.start_ocr_retry, db.start_reingestion = AsyncMock(), AsyncMock()
    monkeypatch.setattr(action_routes, "db", db)
    monkeypatch.setattr(action_routes.workflow_runtime, "db", db)
    monkeypatch.setattr(action_routes.workflow_runtime, "start_ocr_retry", db.start_ocr_retry)
    monkeypatch.setattr(action_routes.workflow_runtime, "start_reingestion", db.start_reingestion)
    monkeypatch.setattr(
        action_routes.access, "require_document_for_user", lambda wf, user, permission: {"workflow_id": wf, **doc}
    )
    return SimpleNamespace(db=db, doc=doc)


def _retry(**flags):
    return asyncio.run(action_routes.retry_ocr("wf-1", OPERATOR, **flags))


def _pages(numbers, text="page {}"):
    return [{"page_number": n, "original_markdown": text.format(n)} for n in numbers]


def _page1(original, edited=None, reviewed=False, **extra):
    return {"page_number": 1, "original_markdown": original, "edited_markdown": edited, "is_reviewed": reviewed, **extra}


def _segment(pages, total, start=None):
    """An OCR engine that announces ``start`` (if given), then OCRs ``pages`` as one segment of ``total``."""

    def engine(on_segment_start, on_segment_complete, **_):
        if start:
            on_segment_start(*start)
        on_segment_complete(pages, total)
        return pages

    return engine


@pytest.fixture
def ocr(db_connection, monkeypatch, tmp_path, minio_uploads):
    """``ocr(engine, **flags)`` runs ``run_ocr_and_store`` for ``WF`` with ``engine`` as the OCR engine.

    ``ocr.calls`` holds each engine call's kwargs (``completed_page_numbers`` copied at call
    time); ``ocr.ticks()`` the ``ocr`` heartbeats.
    """
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    calls, heartbeats = [], []
    monkeypatch.delenv("OCR_SEGMENT_PAGES", raising=False)
    monkeypatch.setattr(activities, "_ensure_pdf_input", lambda path: (str(pdf), False))
    monkeypatch.setattr(activities, "_validate_ocr_pages_for_pdf", lambda *a, **k: None)
    monkeypatch.setattr(activities.activity, "heartbeat", lambda payload=None, **_: heartbeats.append(payload or {}))

    def run(engine, **flags):
        def segments(path, **kw):
            calls.append({**kw, "completed_page_numbers": set(kw["completed_page_numbers"] or ())})
            return engine(**kw)

        monkeypatch.setattr(activities, "_ocr_pdf_in_segments", segments)
        return asyncio.run(activities.run_ocr_and_store(WF, str(pdf), **flags))

    run.calls = calls
    run.ticks = lambda: [h for h in heartbeats if h.get("phase") == "ocr"]
    return run


def test_available_actions_offer_force_ocr_on_ocr_review():
    actions = _actions("ocr_review", page_count=3, ocr_completed_at="2026-01-01T00:00:00")
    assert {"approve_ocr", "force_ocr"} <= actions
    assert "retry_ocr" not in actions


def test_available_actions_resume_and_force_on_failed_partial_ocr():
    assert {"retry_ocr", "force_ocr"} <= _actions("failed", page_count=5, ocr_completed_at=None)


def test_available_actions_suppress_reingest_during_force_ocr_rebuild():
    actions = _actions("ocr_review", page_count=3, **FORCE_REBUILD)
    assert "reingest_document" not in actions
    assert "clear_reindex_required" in actions


def test_available_actions_hide_reingest_for_completed_doc_under_force_rebuild():
    assert "reingest_document" not in _actions("completed", chunks_completed_at=None, **FORCE_REBUILD)
    # Once chunking rebuilt the chunk set, republishing is safe again.
    assert "reingest_document" in _actions("completed", chunks_completed_at="2026-01-02T00:00:00", **FORCE_REBUILD)


def test_available_actions_hide_approve_chunks_while_job_running():
    assert "approve_chunks" not in _actions("chunk_review", {"status": "running", "current_stage": "chunking"})
    assert "approve_chunks" in _actions("chunk_review", {"status": "waiting_review", "current_stage": "chunk_review"})
    assert "approve_chunks" in _actions("chunk_review")


def test_available_actions_hide_all_approvals_while_job_running():
    running, waiting = {"status": "running"}, {"status": "waiting_review"}
    for stage, approval in (
        ("ocr_review", "approve_ocr"),
        ("translation_review", "approve_translation"),
        ("ready_for_ingestion", "approve_ingestion"),
    ):
        assert approval not in _actions(stage, running)
        assert approval in _actions(stage, waiting)
    assert {"force_ocr", "inspect_runtime"} <= _actions("ocr_review", running)
    assert "approve_chunks" not in _actions("translation_review", running)


def test_available_actions_chunking_stage_never_offers_approve_chunks():
    assert "approve_chunks" not in _actions("chunking", {"status": "waiting_review"})


def test_available_actions_treats_unexpected_job_status_as_reviewable():
    assert "approve_chunks" in _actions("chunk_review", {"status": "queued"})
    assert "approve_chunks" in _actions("chunk_review", {"status": None})
    # Admin queue rows alias job status as job_status, not status.
    queue_row = _actions("chunk_review", {"job_status": "running"})
    assert "approve_chunks" not in queue_row
    assert "inspect_runtime" in queue_row


def test_retry_ocr_rejects_discard_without_force(routes):
    with pytest.raises(HTTPException) as exc:
        _retry(force=False, discard_edits=True)
    assert exc.value.status_code == 400
    routes.db.start_ocr_retry.assert_not_called()


def test_retry_ocr_force_forwards_flags(routes):
    result = _retry(force=True, discard_edits=True)
    assert routes.db.start_ocr_retry.call_args.kwargs["args"][-3:] == [True, True, 123]
    assert result["force"] is True
    assert result["discard_edits"] is True
    order = [c[0] for c in routes.db.mock_calls if c[0] in ("create_document_job", "start_ocr_retry")]
    assert order == ["create_document_job", "start_ocr_retry"]


def test_retry_ocr_force_marks_downstream_state_stale_before_workflow_start(routes):
    routes.doc.update(chunk_count=9, reindex_required=0, reindex_reason=None, index="tenant-a-index")
    at_start = {}
    routes.db.start_ocr_retry.side_effect = lambda **_: at_start.update(routes.doc)

    result = _retry(force=True, discard_edits=False)

    assert result["status"] == "started"
    names = [c[0] for c in routes.db.mock_calls]
    assert names.index("update_document_fields") < names.index("start_ocr_retry")
    first_update = routes.db.update_document_fields.call_args_list[0].kwargs
    for fields in (at_start, first_update):
        assert fields["reindex_required"] == 1
        assert fields["reindex_reason"] == "force_ocr_requested"
        assert fields["chunk_count"] == 0
    index_status = routes.db.upsert_document_index_status.call_args.kwargs
    assert index_status["status"] == "stale"
    assert index_status["chunk_count_indexed"] == 0
    assert index_status["index_name"] == "tenant-a-physical-index"


def test_retry_ocr_force_start_failure_restores_prior_state(routes):
    prior = {
        "chunk_count": 7,
        "translation_completed_at": "2026-01-01T00:00:00",
        "chunks_completed_at": "2026-01-02T00:00:00",
        "ingested_at": "2026-01-03T00:00:00",
        "reindex_required": 0,
        "reindex_reason": None,
    }
    routes.doc.update(prior)
    routes.db.start_ocr_retry.side_effect = RuntimeError("forced start failure")

    with pytest.raises(RuntimeError, match="forced start failure"):
        _retry(force=True, discard_edits=False)

    assert {k: routes.doc[k] for k in prior} == prior
    assert routes.doc["error_message"] == "forced start failure"
    job_update = routes.db.update_document_job.call_args
    assert job_update.args[0] == 123
    assert job_update.kwargs["status"] == "failed"


def test_retry_ocr_force_marks_all_existing_index_rows_stale(routes):
    routes.db.list_document_index_status.return_value = [
        {"index_name": "tenant-a-v1", "marqo_doc_id": "doc-1", "schema_version": "passage-v1"},
        {"index_name": "tenant-a-v2", "marqo_doc_id": "doc-1", "schema_version": "passage-v2"},
    ]
    _retry(force=True, discard_edits=False)
    rows = [c.kwargs for c in routes.db.upsert_document_index_status.call_args_list]
    assert {row["index_name"] for row in rows} == {"tenant-a-v1", "tenant-a-v2"}
    assert all(row["status"] == "stale" for row in rows)


@pytest.mark.parametrize(
    "doc",
    [
        {"stage": "ocr_review"},
        # Force can be issued from `completed`; the stage lags until the worker moves it.
        {"stage": "completed", "chunks_completed_at": None, "chunk_count": 0},
    ],
    ids=["ocr_review", "completed_before_worker_moves_it"],
)
def test_reingest_blocked_during_force_ocr_rebuild(routes, doc):
    routes.doc.update(doc, **FORCE_REBUILD)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(action_routes.reingest_document("wf-1", OPERATOR))
    assert exc.value.status_code == 409
    routes.db.start_reingestion.assert_not_called()


def test_force_ocr_audit_records_actor_and_scope(routes):
    routes.db.create_document_job.return_value = 77
    _retry(force=True, discard_edits=False)
    forced = routes.db.log_audit.call_args.kwargs
    assert forced["action_type"] == "force_ocr"
    assert forced["metadata"]["actor"] == "operator-1"
    assert forced["metadata"]["scope"] == "all_pages"
    assert forced["metadata"]["force"] is True
    assert forced["metadata"]["discard_edits"] is False

    _retry(force=False)
    resumed = routes.db.log_audit.call_args.kwargs
    assert resumed["action_type"] == "retry_ocr"
    assert resumed["metadata"]["actor"] == "operator-1"
    assert resumed["metadata"]["scope"] == "resume_missing_pages"


def test_run_ocr_and_store_resume_skips_saved_pages(ocr, db_connection):
    seed_document(db_connection, WF, stage="ocr_processing", pages=["already saved"])
    result = ocr(lambda **_: [], force_redo=False)
    assert result["page_count"] == 1
    assert 1 in ocr.calls[0]["completed_page_numbers"]
    assert db_connection.get_pages(WF)[0]["original_markdown"] == "already saved"


def test_run_ocr_and_store_heartbeats_zero_of_n_before_sqlite_write(ocr, db_connection):
    seed_document(db_connection, WF, stage="ocr_processing")
    in_sqlite_at_start = []

    def engine(on_segment_start, on_segment_complete, **_):
        on_segment_start(0, 5, 5)
        in_sqlite_at_start.extend(row["page_number"] for row in db_connection.get_pages(WF))
        on_segment_complete(_pages(range(1, 6)), 5)
        return _pages(range(1, 6))

    result = ocr(engine, force_redo=False)
    ticks = ocr.ticks()
    assert in_sqlite_at_start == []
    assert (ticks[0]["done"], ticks[0]["total"]) == (0, 5)
    assert (ticks[-1]["done"], ticks[-1]["total"]) == (5, 5)
    assert result["page_count"] == 5
    assert sorted(row["page_number"] for row in db_connection.get_pages(WF)) == [1, 2, 3, 4, 5]


def test_run_ocr_and_store_resume_start_heartbeat_uses_saved_count(ocr, db_connection):
    seed_document(db_connection, WF, stage="ocr_processing", page_count=5, pages=_pages(range(1, 6), "saved {}"))
    ocr(_segment(_pages(range(6, 9)), 8, start=(5, 8, 8)), force_redo=False)
    ticks = ocr.ticks()
    assert (ticks[0]["done"], ticks[0]["total"]) == (5, 8)
    assert ticks[-1]["done"] == 8
    assert sorted(row["page_number"] for row in db_connection.get_pages(WF)) == list(range(1, 9))
    assert db_connection.get_page(WF, 1)["original_markdown"] == "saved 1"


def test_run_ocr_and_store_default_segment_pages_is_five(ocr, db_connection):
    seed_document(db_connection, WF, stage="ocr_processing")
    ocr(_segment(_pages([1]), 1), force_redo=False)
    assert ocr.calls[0]["segment_pages"] == 5


def test_run_ocr_and_store_force_replaces_pages_and_keeps_edits(ocr, db_connection):
    old = _page1("bad ocr", "kept edit", True, reviewer_notes="note")
    seed_document(db_connection, WF, stage="ocr_review", page_count=1, pages=[old])

    result = ocr(_segment([_page1("fresh ocr", reviewer_notes=None)], 1), force_redo=True, discard_edits=False)

    assert ocr.calls[0]["completed_page_numbers"] == set()
    assert result["page_count"] == 1
    page = db_connection.get_page(WF, 1)
    assert page["original_markdown"] == "fresh ocr"
    assert page["edited_markdown"] == "kept edit"
    assert page["is_reviewed"] is False
    assert page["reviewer_notes"] == "note"


def test_run_ocr_and_store_force_discard_edits(ocr, db_connection):
    seed_document(db_connection, WF, stage="ocr_review", page_count=1, pages=[_page1("bad ocr", "should drop", True)])

    ocr(_segment([_page1("fresh ocr")], 1), force_redo=True, discard_edits=True)

    page = db_connection.get_page(WF, 1)
    assert page["original_markdown"] == "fresh ocr"
    assert page["edited_markdown"] is None
    assert page["is_reviewed"] is False


def test_run_ocr_and_store_force_init_is_one_time_across_retries(ocr, db_connection):
    old = _page1("old bad ocr", "operator keep me", True, reviewer_notes="keep notes")
    seed_document(db_connection, WF, stage="ocr_review", page_count=1, pages=[old])
    job_id = db_connection.create_document_job(
        workflow_id=WF, job_type="ocr_retry", status="running", current_stage="ocr_processing",
        config={"source": "api_retry_ocr", "force": True, "discard_edits": False},
    )
    db_connection.update_document_fields(WF, latest_job_id=job_id)

    def crash_after_first_segment(on_segment_complete, **_):
        on_segment_complete(_pages([1], "fresh ocr attempt one"), 1)
        raise RuntimeError("forced crash after first segment")

    with pytest.raises(RuntimeError, match="forced crash"):
        ocr(crash_after_first_segment, force_redo=True, discard_edits=False)

    job_cfg = json.loads(db_connection.get_document_job(job_id).get("config_json") or "{}")
    assert job_cfg["force_ocr_state"]["initialized"] is True
    assert "1" in job_cfg["force_ocr_state"]["edit_snapshot"]

    result = ocr(lambda **_: [], force_redo=True, discard_edits=False)
    assert result["page_count"] == 1
    assert ocr.calls[1]["completed_page_numbers"] == {1}
    page = db_connection.get_page(WF, 1)
    assert page["original_markdown"] == "fresh ocr attempt one"
    assert page["edited_markdown"] == "operator keep me"
