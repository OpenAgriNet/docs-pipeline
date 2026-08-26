"""OCR resume vs force redo (#123)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from pipeline.services.documents import list_available_actions


@pytest.mark.unit
def test_available_actions_offer_force_ocr_on_ocr_review():
    actions = list_available_actions(
        {
            "stage": "ocr_review",
            "page_count": 3,
            "ocr_completed_at": "2026-01-01T00:00:00",
            "is_disabled": False,
        }
    )
    assert "approve_ocr" in actions
    assert "force_ocr" in actions
    assert "retry_ocr" not in actions


@pytest.mark.unit
def test_available_actions_resume_and_force_on_failed_partial_ocr():
    actions = list_available_actions(
        {
            "stage": "failed",
            "page_count": 5,
            "ocr_completed_at": None,
            "is_disabled": False,
        }
    )
    assert "retry_ocr" in actions
    assert "force_ocr" in actions


@pytest.mark.unit
def test_available_actions_suppress_reingest_during_force_ocr_rebuild():
    actions = list_available_actions(
        {
            "stage": "ocr_review",
            "page_count": 3,
            "is_disabled": False,
            "reindex_required": 1,
            "reindex_reason": "force_ocr_requested",
        }
    )
    assert "reingest_document" not in actions
    assert "clear_reindex_required" in actions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_ocr_rejects_discard_without_force(monkeypatch):
    from pipeline.routers import documents_actions as action_routes

    monkeypatch.setattr(
        action_routes.access,
        "require_document_for_user",
        lambda workflow_id, user, permission: {
            "workflow_id": workflow_id,
            "document_id": "doc-1",
            "filename": "doc.pdf",
            "filepath": "/tmp/doc.pdf",
            "instance": "default",
        },
    )
    called = {"start": False}

    async def _never_called(**kwargs):
        called["start"] = True

    monkeypatch.setattr(action_routes.workflow_runtime, "start_ocr_retry", _never_called)

    with pytest.raises(HTTPException) as exc:
        await action_routes.retry_ocr("wf-1", object(), force=False, discard_edits=True)
    assert exc.value.status_code == 400
    assert called["start"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_ocr_force_forwards_flags(monkeypatch):
    from pipeline.routers import documents_actions as action_routes

    monkeypatch.setattr(
        action_routes.access,
        "require_document_for_user",
        lambda workflow_id, user, permission: {
            "workflow_id": workflow_id,
            "document_id": "doc-1",
            "filename": "doc.pdf",
            "filepath": "/tmp/doc.pdf",
            "instance": "tenant-a",
        },
    )
    captured: dict = {}

    async def _capture_start(**kwargs):
        call_order.append("start")
        captured["start"] = kwargs

    monkeypatch.setattr(action_routes.workflow_runtime, "start_ocr_retry", _capture_start)
    call_order: list[str] = []

    def _create_job(**kwargs):
        call_order.append("create_job")
        return 123

    monkeypatch.setattr(action_routes.db, "create_document_job", _create_job)
    monkeypatch.setattr(action_routes.db, "update_document_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(action_routes.db, "upsert_document_index_status", lambda **kwargs: None)
    monkeypatch.setattr(action_routes.db, "list_document_index_status", lambda _workflow_id: [])
    monkeypatch.setattr(
        action_routes.db,
        "resolve_ingest_index_name",
        lambda instance, logical: "tenant-a-physical-index",
    )
    monkeypatch.setattr(action_routes.db, "log_audit", lambda **kwargs: captured.setdefault("audit", kwargs))

    result = await action_routes.retry_ocr(
        "wf-1", object(), force=True, discard_edits=True
    )
    assert captured["start"]["args"][-3:] == [True, True, 123]
    assert result["force"] is True
    assert result["discard_edits"] is True
    assert call_order == ["create_job", "start"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_ocr_force_marks_downstream_state_stale(monkeypatch):
    from pipeline.routers import documents_actions as action_routes

    monkeypatch.setattr(
        action_routes.access,
        "require_document_for_user",
        lambda workflow_id, user, permission: {
            "workflow_id": workflow_id,
            "document_id": "doc-1",
            "filename": "doc.pdf",
            "filepath": "/tmp/doc.pdf",
            "instance": "tenant-a",
            "index": "tenant-a-index",
        },
    )
    captured: dict = {}

    async def _capture_start(**kwargs):
        captured["start"] = kwargs

    monkeypatch.setattr(action_routes.workflow_runtime, "start_ocr_retry", _capture_start)
    monkeypatch.setattr(action_routes.db, "create_document_job", lambda **kwargs: 123)
    monkeypatch.setattr(
        action_routes.db,
        "update_document_fields",
        lambda workflow_id, **kwargs: captured.setdefault("fields", kwargs),
    )
    monkeypatch.setattr(action_routes.db, "list_document_index_status", lambda _workflow_id: [])
    monkeypatch.setattr(
        action_routes.db,
        "resolve_ingest_index_name",
        lambda instance, logical: "tenant-a-physical-index",
    )
    monkeypatch.setattr(
        action_routes.db,
        "upsert_document_index_status",
        lambda **kwargs: captured.setdefault("index_status", kwargs),
    )
    monkeypatch.setattr(action_routes.db, "log_audit", lambda **kwargs: None)

    await action_routes.retry_ocr("wf-1", object(), force=True, discard_edits=False)

    assert captured["fields"]["reindex_required"] == 1
    assert captured["fields"]["reindex_reason"] == "force_ocr_requested"
    assert captured["fields"]["chunk_count"] == 0
    assert captured["index_status"]["status"] == "stale"
    assert captured["index_status"]["chunk_count_indexed"] == 0
    assert captured["index_status"]["index_name"] == "tenant-a-physical-index"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_ocr_force_marks_all_existing_index_rows_stale(monkeypatch):
    from pipeline.routers import documents_actions as action_routes

    monkeypatch.setattr(
        action_routes.access,
        "require_document_for_user",
        lambda workflow_id, user, permission: {
            "workflow_id": workflow_id,
            "document_id": "doc-1",
            "filename": "doc.pdf",
            "filepath": "/tmp/doc.pdf",
            "instance": "tenant-a",
            "index": "logical-index",
        },
    )
    captured_rows: list[dict] = []

    async def _capture_start(**kwargs):
        return None

    monkeypatch.setattr(action_routes.workflow_runtime, "start_ocr_retry", _capture_start)
    monkeypatch.setattr(action_routes.db, "create_document_job", lambda **kwargs: 123)
    monkeypatch.setattr(action_routes.db, "update_document_fields", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        action_routes.db,
        "list_document_index_status",
        lambda _workflow_id: [
            {"index_name": "tenant-a-v1", "marqo_doc_id": "doc-1", "schema_version": "passage-v1"},
            {"index_name": "tenant-a-v2", "marqo_doc_id": "doc-1", "schema_version": "passage-v2"},
        ],
    )
    monkeypatch.setattr(
        action_routes.db,
        "upsert_document_index_status",
        lambda **kwargs: captured_rows.append(kwargs),
    )
    monkeypatch.setattr(action_routes.db, "log_audit", lambda **kwargs: None)

    await action_routes.retry_ocr("wf-1", object(), force=True, discard_edits=False)

    assert {row["index_name"] for row in captured_rows} == {"tenant-a-v1", "tenant-a-v2"}
    assert all(row["status"] == "stale" for row in captured_rows)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reingest_blocked_while_force_ocr_rebuild(monkeypatch):
    from pipeline.routers import documents_actions as action_routes

    monkeypatch.setattr(
        action_routes.access,
        "require_document_for_user",
        lambda workflow_id, user, permission: {
            "workflow_id": workflow_id,
            "document_id": "doc-1",
            "filename": "doc.pdf",
            "filepath": "/tmp/doc.pdf",
            "instance": "tenant-a",
            "stage": "ocr_review",
            "reindex_required": 1,
            "reindex_reason": "force_ocr_requested",
        },
    )
    monkeypatch.setattr(action_routes.db, "get_chunks", lambda workflow_id, include_excluded=False: [{"chunk_number": 1}])
    started = {"called": False}

    async def _start_reingest(**kwargs):
        started["called"] = True

    monkeypatch.setattr(action_routes.workflow_runtime, "start_reingestion", _start_reingest)

    with pytest.raises(HTTPException) as exc:
        await action_routes.reingest_document("wf-1", object())
    assert exc.value.status_code == 409
    assert started["called"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_ocr_and_store_resume_skips_saved_pages(db_connection, monkeypatch, tmp_path):
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-ocr-resume"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-resume",
        filename="doc.pdf",
        filepath="/tmp/doc.pdf",
        stage="ocr_processing",
    )
    db_connection.save_pages(
        workflow_id,
        [{"page_number": 1, "original_markdown": "already saved"}],
    )

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    called = {}

    def fake_segments(path, segment_pages=20, on_segment_complete=None, completed_page_numbers=None):
        called["completed"] = set(completed_page_numbers or set())
        # Resume path should see page 1 as already done.
        return []

    monkeypatch.setattr(activities, "_ensure_pdf_input", lambda path: (str(pdf_path), False))
    monkeypatch.setattr(activities, "_ocr_pdf_in_segments", fake_segments)
    monkeypatch.setattr(activities, "_validate_ocr_pages_for_pdf", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        activities,
        "_upload_file_to_minio",
        lambda *args, **kwargs: ("s3://b/k", 1, "application/pdf"),
    )
    monkeypatch.setattr(activities, "_write_json_temp", lambda data: str(tmp_path / "pages.json"))
    (tmp_path / "pages.json").write_text("[]", encoding="utf-8")

    result = await activities.run_ocr_and_store(workflow_id, str(pdf_path), force_redo=False)
    assert result["page_count"] == 1
    assert 1 in called["completed"]
    assert db_connection.get_pages(workflow_id)[0]["original_markdown"] == "already saved"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_ocr_and_store_force_replaces_pages_and_keeps_edits(
    db_connection, monkeypatch, tmp_path
):
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-ocr-force"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-force",
        filename="doc.pdf",
        filepath="/tmp/doc.pdf",
        stage="ocr_review",
        page_count=1,
    )
    db_connection.save_pages(
        workflow_id,
        [
            {
                "page_number": 1,
                "original_markdown": "bad ocr",
                "edited_markdown": "kept edit",
                "is_reviewed": True,
                "reviewer_notes": "note",
            }
        ],
    )

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    def fake_segments(path, segment_pages=20, on_segment_complete=None, completed_page_numbers=None):
        assert set(completed_page_numbers or set()) == set()
        pages = [
            {
                "page_number": 1,
                "original_markdown": "fresh ocr",
                "edited_markdown": None,
                "is_reviewed": False,
                "reviewer_notes": None,
            }
        ]
        if on_segment_complete:
            on_segment_complete(pages, 1)
        return pages

    monkeypatch.setattr(activities, "_ensure_pdf_input", lambda path: (str(pdf_path), False))
    monkeypatch.setattr(activities, "_ocr_pdf_in_segments", fake_segments)
    monkeypatch.setattr(activities, "_validate_ocr_pages_for_pdf", lambda *args, **kwargs: None)
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        activities,
        "_upload_file_to_minio",
        lambda *args, **kwargs: ("s3://b/k", 1, "application/pdf"),
    )
    monkeypatch.setattr(activities, "_write_json_temp", lambda data: str(tmp_path / "pages.json"))
    (tmp_path / "pages.json").write_text("[]", encoding="utf-8")

    result = await activities.run_ocr_and_store(
        workflow_id, str(pdf_path), force_redo=True, discard_edits=False
    )
    assert result["page_count"] == 1
    page = db_connection.get_page(workflow_id, 1)
    assert page["original_markdown"] == "fresh ocr"
    assert page["edited_markdown"] == "kept edit"
    assert page["is_reviewed"] is False
    assert page["reviewer_notes"] == "note"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_ocr_and_store_force_discard_edits(db_connection, monkeypatch, tmp_path):
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-ocr-force-discard"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-force-discard",
        filename="doc.pdf",
        filepath="/tmp/doc.pdf",
        stage="ocr_review",
        page_count=1,
    )
    db_connection.save_pages(
        workflow_id,
        [
            {
                "page_number": 1,
                "original_markdown": "bad ocr",
                "edited_markdown": "should drop",
                "is_reviewed": True,
            }
        ],
    )

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    def fake_segments(path, segment_pages=20, on_segment_complete=None, completed_page_numbers=None):
        pages = [
            {
                "page_number": 1,
                "original_markdown": "fresh ocr",
                "edited_markdown": None,
                "is_reviewed": False,
            }
        ]
        if on_segment_complete:
            on_segment_complete(pages, 1)
        return pages

    monkeypatch.setattr(activities, "_ensure_pdf_input", lambda path: (str(pdf_path), False))
    monkeypatch.setattr(activities, "_ocr_pdf_in_segments", fake_segments)
    monkeypatch.setattr(activities, "_validate_ocr_pages_for_pdf", lambda *args, **kwargs: None)
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        activities,
        "_upload_file_to_minio",
        lambda *args, **kwargs: ("s3://b/k", 1, "application/pdf"),
    )
    monkeypatch.setattr(activities, "_write_json_temp", lambda data: str(tmp_path / "pages.json"))
    (tmp_path / "pages.json").write_text("[]", encoding="utf-8")

    await activities.run_ocr_and_store(
        workflow_id, str(pdf_path), force_redo=True, discard_edits=True
    )
    page = db_connection.get_page(workflow_id, 1)
    assert page["original_markdown"] == "fresh ocr"
    assert page["edited_markdown"] is None
    assert page["is_reviewed"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_ocr_and_store_force_init_is_one_time_across_retries(
    db_connection, monkeypatch, tmp_path
):
    import json

    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-ocr-force-retry-state"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-force-retry-state",
        filename="doc.pdf",
        filepath="/tmp/doc.pdf",
        stage="ocr_review",
        page_count=1,
    )
    db_connection.save_pages(
        workflow_id,
        [
            {
                "page_number": 1,
                "original_markdown": "old bad ocr",
                "edited_markdown": "operator keep me",
                "is_reviewed": True,
                "reviewer_notes": "keep notes",
            }
        ],
    )
    job_id = db_connection.create_document_job(
        workflow_id=workflow_id,
        job_type="ocr_retry",
        status="running",
        current_stage="ocr_processing",
        config={"source": "api_retry_ocr", "force": True, "discard_edits": False},
    )
    db_connection.update_document_fields(workflow_id, latest_job_id=job_id)

    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    calls = {"count": 0, "completed": []}

    def fake_segments(path, segment_pages=20, on_segment_complete=None, completed_page_numbers=None):
        calls["count"] += 1
        calls["completed"].append(set(completed_page_numbers or set()))
        if calls["count"] == 1:
            pages = [{"page_number": 1, "original_markdown": "fresh ocr attempt one"}]
            if on_segment_complete:
                on_segment_complete(pages, 1)
            raise RuntimeError("forced crash after first segment")
        return []

    monkeypatch.setattr(activities, "_ensure_pdf_input", lambda path: (str(pdf_path), False))
    monkeypatch.setattr(activities, "_ocr_pdf_in_segments", fake_segments)
    monkeypatch.setattr(activities, "_validate_ocr_pages_for_pdf", lambda *args, **kwargs: None)
    monkeypatch.setattr(activities.activity, "heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        activities,
        "_upload_file_to_minio",
        lambda *args, **kwargs: ("s3://b/k", 1, "application/pdf"),
    )
    monkeypatch.setattr(activities, "_write_json_temp", lambda data: str(tmp_path / "pages.json"))
    (tmp_path / "pages.json").write_text("[]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="forced crash"):
        await activities.run_ocr_and_store(
            workflow_id,
            str(pdf_path),
            force_redo=True,
            discard_edits=False,
        )

    job = db_connection.get_document_job(job_id)
    job_cfg = json.loads(job.get("config_json") or "{}")
    assert job_cfg["force_ocr_state"]["initialized"] is True
    assert "1" in job_cfg["force_ocr_state"]["edit_snapshot"]

    result = await activities.run_ocr_and_store(
        workflow_id,
        str(pdf_path),
        force_redo=True,
        discard_edits=False,
    )
    assert result["page_count"] == 1
    assert calls["completed"][1] == {1}
    page = db_connection.get_page(workflow_id, 1)
    assert page["original_markdown"] == "fresh ocr attempt one"
    assert page["edited_markdown"] == "operator keep me"
