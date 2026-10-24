"""SQLite layer (``pipeline/db.py``): documents, pages, chunks, audit, settings, index selection."""

import pytest

from tests.helpers import seed_document

WF = "wf-db"


@pytest.fixture
def db(db_connection):
    return db_connection


# --- documents --------------------------------------------------------------------


def test_upsert_document_creates_new(db):
    seed_document(db, WF, document_id="doc-001", filename="test.pdf")
    doc = db.get_document(WF)
    assert doc is not None
    assert doc["workflow_id"] == WF
    assert doc["document_id"] == "doc-001"
    assert doc["filename"] == "test.pdf"
    assert doc["stage"] == "registered"


def test_upsert_document_updates_existing(db):
    seed_document(db, WF)
    seed_document(db, WF, stage="ocr_processing")
    assert db.get_document(WF)["stage"] == "ocr_processing"


def test_get_document_not_found(db):
    assert db.get_document("nonexistent-workflow") is None


def test_list_documents(db):
    for i in range(3):
        seed_document(db, f"list-test-{i}")
    assert sorted(d["workflow_id"] for d in db.list_documents()) == ["list-test-0", "list-test-1", "list-test-2"]
    assert db.count_documents() == 3


def test_list_documents_with_stage_filter(db):
    seed_document(db, "wf-done", stage="completed")
    seed_document(db, "wf-failed", stage="failed")
    assert [d["workflow_id"] for d in db.list_documents(stage="completed")] == ["wf-done"]
    assert db.count_documents(stage="completed") == 1
    assert db.count_documents(stage="failed") == 1


def test_update_document_stage(db):
    seed_document(db, WF)
    db.update_document_stage(workflow_id=WF, stage="ocr_review", page_count=5, chunk_count=10)
    doc = db.get_document(WF)
    assert doc["stage"] == "ocr_review"
    assert doc["page_count"] == 5
    assert doc["chunk_count"] == 10


@pytest.mark.parametrize("setter, column", [("set_document_demo", "is_demo"), ("set_document_disabled", "is_disabled")])
def test_document_flag_setter(db, setter, column):
    seed_document(db, WF)
    getattr(db, setter)(WF, True)
    assert db.get_document(WF)[column] == 1


# --- pages ------------------------------------------------------------------------


def test_persist_pages(db):
    seed_document(db, WF)
    db.persist_document_content(workflow_id=WF, chunks=[], pages=[
        {"page_number": 1, "original_markdown": "# Page 1", "detected_language": "en"},
        {"page_number": 2, "original_markdown": "# Page 2", "detected_language": "en"},
    ])
    stored = db.get_pages(WF)
    assert len(stored) == 2
    assert stored[0]["original_markdown"] == "# Page 1"


def test_delete_pages_removes_specific_or_all_rows(db):
    seed_document(db, WF, pages=["one", "two"])
    assert db.delete_pages(WF, [1]) == 1
    assert [p["page_number"] for p in db.get_pages(WF)] == [2]
    assert db.delete_pages(WF) == 1
    assert db.get_pages(WF) == []


def test_snapshot_and_restore_page_ocr_edits(db):
    """Operator edits survive a re-OCR; the review flag is reset for the fresh text."""
    edited = {"page_number": 1, "original_markdown": "machine", "edited_markdown": "operator edit",
              "is_reviewed": True, "reviewer_notes": "fixed heading"}
    seed_document(db, WF, pages=[edited, "plain"])
    snapshot = db.snapshot_page_ocr_edits(WF)
    assert set(snapshot) == {1}
    assert snapshot[1]["edited_markdown"] == "operator edit"

    db.delete_pages(WF)
    db.save_pages(WF, [{"page_number": 1, "original_markdown": "fresh ocr"},
                       {"page_number": 2, "original_markdown": "fresh two"}])
    assert db.restore_page_ocr_edits(WF, snapshot) == 1
    page = db.get_page(WF, 1)
    assert page["original_markdown"] == "fresh ocr"
    assert page["edited_markdown"] == "operator edit"
    assert page["is_reviewed"] is False
    assert page["reviewer_notes"] == "fixed heading"


def test_get_page(db):
    seed_document(db, WF, pages=["Test"])
    page = db.get_page(WF, 1)
    assert page is not None
    assert page["page_number"] == 1


def test_update_page(db):
    seed_document(db, WF, pages=["Original"])
    db.update_page(workflow_id=WF, page_num=1, edited_markdown="Edited content", is_reviewed=True)
    page = db.get_page(WF, 1)
    assert page["edited_markdown"] == "Edited content"
    assert page["is_reviewed"] == 1


def test_reset_page(db):
    seed_document(db, WF, pages=["Original"])
    db.update_page(workflow_id=WF, page_num=1, edited_markdown="Edited")
    db.reset_page(WF, 1)
    assert db.get_page(WF, 1)["edited_markdown"] is None


# --- chunks -----------------------------------------------------------------------


def test_persist_chunks(db):
    seed_document(db, WF)
    db.persist_document_content(workflow_id=WF, pages=[], chunks=[
        {"chunk_number": 1, "original_text": "Chunk 1 text", "source_pages": [1], "token_count": 5},
        {"chunk_number": 2, "original_text": "Chunk 2 text", "source_pages": [1, 2], "token_count": 5},
    ])
    assert len(db.get_chunks(WF)) == 2


def test_get_chunks_excludes_excluded_by_default(db):
    seed_document(db, WF, chunks=["Included", "Excluded"])
    db.update_chunk(workflow_id=WF, chunk_num=2, is_excluded=True)
    assert len(db.get_chunks(WF)) == 1
    assert len(db.get_chunks(WF, include_excluded=True)) == 2


def test_update_chunk(db):
    seed_document(db, WF, chunks=["Original"])
    db.update_chunk(workflow_id=WF, chunk_num=1, edited_text="Edited text", is_reviewed=True)
    chunk = db.get_chunk(WF, 1)
    assert chunk["edited_text"] == "Edited text"
    assert chunk["is_reviewed"] == 1


# --- audit ------------------------------------------------------------------------


def test_log_audit(db):
    seed_document(db, WF)
    db.log_audit(workflow_id=WF, document_id=f"doc-{WF}", action_type="test_action",
                 entity_type="document", field_name="stage", old_value="old", new_value="new")
    assert [log["action_type"] for log in db.get_audit_logs(WF)] == ["test_action"]


def test_get_audit_logs_with_filter(db):
    seed_document(db, WF)
    for action in ("type_a", "type_b"):
        db.log_audit(workflow_id=WF, document_id=f"doc-{WF}", action_type=action)
    assert [log["action_type"] for log in db.get_audit_logs(WF, action_type="type_a")] == ["type_a"]


def test_get_all_audit_logs(db):
    seed_document(db, WF)
    db.log_audit(workflow_id=WF, document_id=f"doc-{WF}", action_type="global_test")
    assert [log["action_type"] for log in db.get_all_audit_logs()] == ["global_test"]


# --- settings ---------------------------------------------------------------------


def test_get_search_settings_defaults(db):
    settings = db.get_search_settings()
    assert "searchMethod" in settings
    assert "limit" in settings
    assert "alpha" in settings


def test_update_search_settings(db):
    db.update_search_settings({"limit": 25})
    assert db.get_search_settings()["limit"] == 25


def test_settings_audit_log(db):
    db.update_search_settings({"limit": 50})
    assert len(db.get_settings_audit_logs()) == 1


# --- index-scoped workflow selection (Indexes console reindex) --------------------


def test_list_workflow_ids_for_index_scopes_and_modes(db):
    seed_document(db, "wf-a-stale", stage="completed", instance="tenant-a")
    seed_document(db, "wf-a-ok", stage="completed", instance="tenant-a")
    seed_document(db, "wf-b", stage="completed", instance="tenant-b")
    db.mark_document_reindex_required("wf-a-stale", True, "schema drift")
    db.upsert_document_index_status("wf-a-stale", "idx-a", status="indexed")
    db.upsert_document_index_status("wf-a-ok", "idx-a", status="indexed")
    db.upsert_document_index_status("wf-b", "idx-b", status="indexed")

    assert db.list_workflow_ids_for_index("idx-a", mode="stale") == ["wf-a-stale"]
    assert set(db.list_workflow_ids_for_index("idx-a", mode="all")) == {"wf-a-stale", "wf-a-ok"}
    assert db.list_workflow_ids_for_index("idx-a", mode="all", instances=["tenant-b"]) == []
    assert db.list_workflow_ids_for_index("idx-b", mode="all") == ["wf-b"]
