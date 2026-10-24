"""HTTP routes through the FastAPI TestClient: health, documents, pages, chunks, audit,
settings, errors, and upload validation."""

import pytest

from pipeline.services.documents import inline_content_disposition
from tests.helpers import seed_document

WF = "wf-api"
MAGIC_PREFIXED_JUNK = b"%PDF-1.4 FINAL AUDIT reproduction body"


@pytest.fixture
def doc(test_client, db_connection):
    """A completed document with two pages and two chunks."""
    seed_document(db_connection, WF, stage="completed", pages=["Page 1", "Page 2"], chunks=["Included", "Excluded"])
    return WF


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def test_health_endpoint(test_client):
    assert _ok(test_client.get("/health"))["status"] == "ok"


def test_pipeline_stages_endpoint(test_client):
    stages = _ok(test_client.get("/pipeline/stages"))
    assert len(stages) > 0
    assert all("id" in s and "label" in s for s in stages)


# --- documents, pages, chunks ------------------------------------------------------


def test_list_documents(test_client, doc):
    payload = _ok(test_client.get("/documents"))
    assert isinstance(payload, dict)
    assert isinstance(payload["items"], list)
    assert payload["total"] >= 1
    assert payload["limit"] == 100
    assert payload["offset"] == 0
    assert any(d["workflow_id"] == doc for d in payload["items"])


def test_get_document_from_db(test_client, doc):
    assert _ok(test_client.get(f"/documents/{doc}"))["workflow_id"] == doc


def test_list_pages(test_client, doc):
    assert len(_ok(test_client.get(f"/documents/{doc}/pages"))) == 2


def test_get_page(test_client, doc):
    assert _ok(test_client.get(f"/documents/{doc}/pages/1"))["page_number"] == 1


@pytest.mark.parametrize("path", ["pages/0", "pages/-1", "chunks/0"])
def test_page_and_chunk_numbers_are_1_indexed(test_client, doc, path):
    assert test_client.get(f"/documents/{doc}/{path}").status_code == 422


def test_update_page(test_client, doc):
    page = _ok(test_client.patch(f"/documents/{doc}/pages/1", json={"edited_markdown": "Updated content"}))
    assert page["edited_markdown"] == "Updated content"


def test_list_chunks(test_client, doc):
    assert len(_ok(test_client.get(f"/documents/{doc}/chunks"))) == 2


def test_list_chunks_include_excluded(test_client, db_connection, doc):
    db_connection.update_chunk(doc, 2, is_excluded=True)
    assert len(test_client.get(f"/documents/{doc}/chunks").json()) == 1
    assert len(test_client.get(f"/documents/{doc}/chunks?include_excluded=true").json()) == 2


# --- audit and settings ------------------------------------------------------------


def test_get_document_audit(test_client, db_connection, doc):
    db_connection.log_audit(workflow_id=doc, document_id=f"doc-{doc}", action_type="test_action")
    data = _ok(test_client.get(f"/documents/{doc}/audit"))
    assert [log["action_type"] for log in data["logs"]] == ["test_action"]
    assert data["total"] == 1


def test_get_global_audit(test_client, db_connection):
    db_connection.log_audit(workflow_id="wf-x", document_id="doc-x", action_type="global_action")
    data = _ok(test_client.get("/audit"))
    assert [log["action_type"] for log in data["logs"]] == ["global_action"]
    assert data["total"] == 1


def test_audit_pagination(test_client, db_connection):
    data = _ok(test_client.get("/audit?limit=5&offset=0"))
    assert data["limit"] == 5
    assert data["offset"] == 0


def test_get_search_settings(test_client, db_connection):
    settings = _ok(test_client.get("/settings/search"))
    assert "searchMethod" in settings
    assert "limit" in settings


def test_update_search_settings(test_client, db_connection):
    assert _ok(test_client.put("/settings/search", json={"limit": 25}))["limit"] == 25


def test_get_settings_audit(test_client, db_connection):
    test_client.put("/settings/search", json={"limit": 30})
    logs = _ok(test_client.get("/settings/search/audit"))["logs"]
    assert len(logs) == 1


# --- errors and headers ------------------------------------------------------------


def test_document_not_found(test_client):
    response = test_client.get("/documents/nonexistent")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_page_not_found(test_client, doc):
    assert test_client.get(f"/documents/{doc}/pages/999").status_code == 404


def test_inline_content_disposition_unicode_filename():
    header = inline_content_disposition("રબર મેટ.pdf")
    header.encode("latin-1")
    assert "filename*=" in header
    assert header.startswith('inline; filename="')


def test_inline_content_disposition_strips_crlf():
    """The filename cannot inject a header line: it stays one Content-Disposition value."""
    header = inline_content_disposition("evil.pdf\r\nX-Injected: yes")
    assert "\r" not in header
    assert "\n" not in header
    assert "\r\nX-Injected" not in header
    assert header.startswith('inline; filename="')


# --- POST /upload validation: extension, magic bytes, structural readability ---------


@pytest.mark.parametrize(
    "name, content, mime, detail",
    [
        ("audit-stub.pdf", MAGIC_PREFIXED_JUNK, "application/pdf", "Invalid PDF file: file is not a structurally readable PDF"),
        ("not-a-pdf.pdf", b"not a pdf", "application/pdf", "Invalid PDF file: file does not have valid PDF header"),
        ("notes.txt", b"hello", "text/plain", "Unsupported file type: .txt"),
    ],
    ids=["magic-prefixed-but-unreadable", "bad-magic-bytes", "unsupported-extension"],
)
def test_bad_upload_rejected_before_storage_or_workflow(
    test_client, mock_minio_client, mock_temporal_client, name, content, mime, detail
):
    response = test_client.post("/upload", files={"file": (name, content, mime)})
    assert response.status_code == 400
    assert response.json()["detail"] == detail
    mock_minio_client.put_object.assert_not_called()
    mock_temporal_client.start_workflow.assert_not_called()


@pytest.mark.parametrize(
    "name, mime, junk",
    [("sample.pdf", "application/pdf", False), ("scan.png", "image/png", True)],
    ids=["readable-pdf", "non-pdf-skips-pdf-checks"],
)
def test_upload_succeeds(
    test_client, mock_minio_client, mock_temporal_client, sample_pdf_content, db_connection, name, mime, junk
):
    """Only a .pdf gets the PDF checks: magic-prefixed junk uploaded as .png is accepted."""
    content = MAGIC_PREFIXED_JUNK if junk else sample_pdf_content
    data = _ok(test_client.post("/upload", files={"file": (name, content, mime)}))
    assert data["filename"] == name
    assert data["source_filename"] == name
    assert data["stage"] == "registered"
    assert data["page_count"] == 0
    assert data["chunk_count"] == 0
    assert data["workflow_id"]
    assert data["document_id"]
    mock_minio_client.put_object.assert_called_once()
    mock_temporal_client.start_workflow.assert_called()
