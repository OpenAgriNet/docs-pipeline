"""OCR resume vs force redo (#123)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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
    assert page["is_reviewed"] is True
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
