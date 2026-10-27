"""GET /pdf must stream a real PDF, not the original Office bytes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from pipeline.routers import documents
from pipeline.services import documents as document_service
from tests.helpers import member, seed_document

WF = "wf-preview-docx"
DOCX = dict(filename="Buffalor Brief.docx", filepath="minio://documents/amul/brief.docx", stage="ocr_review", instance="tenant-a")


@pytest.fixture
def docx_doc(db_connection):
    seed_document(db_connection, WF, **DOCX)
    return WF


def _normalized(db, workflow_id, filename):
    """Record a converted PDF for the document; returns the artifact id."""
    uri = f"minio://documents/tenant-a/{workflow_id}/normalized_pdf/{filename}"
    return db.add_document_artifact(workflow_id, "normalized_pdf", uri, filename=filename, mime_type="application/pdf")


def _preview(db, workflow_id):
    return document_service.preview_pdf_source(db.get_document(workflow_id))


def test_office_preview_serves_the_normalized_pdf(docx_doc, db_connection, monkeypatch):
    _normalized(db_connection, docx_doc, "brief.pdf")
    uri, name = _preview(db_connection, docx_doc)
    assert uri.endswith("brief.pdf")
    assert name.endswith(".pdf")
    assert "brief.docx" not in uri.lower()

    seen = []

    def _get_object(bucket, name):
        seen.append((bucket, name))
        return iter([b"%PDF-1.4 test"])

    monkeypatch.setattr(
        "pipeline.routers.documents.minio_storage.get_client", lambda: SimpleNamespace(get_object=_get_object)
    )
    response = asyncio.run(documents.get_document_pdf(docx_doc, member("tenant-a", "admin")))
    assert response.media_type == "application/pdf"
    assert seen == [("documents", f"tenant-a/{docx_doc}/normalized_pdf/brief.pdf")]


def test_preview_falls_back_to_pdf_filepath(db_connection):
    seed_document(db_connection, "wf-preview-pdf", filename="note.pdf", filepath="minio://documents/amul/note.pdf",
                  stage="ocr_review", instance="tenant-a")
    uri, name = _preview(db_connection, "wf-preview-pdf")
    assert uri.endswith("note.pdf")
    assert name.endswith(".pdf")


@pytest.mark.parametrize("conversion_purged", [False, True], ids=["never-converted", "conversion-purged"])
def test_office_without_a_live_pdf_has_no_preview(docx_doc, db_connection, conversion_purged):
    if conversion_purged:
        db_connection.mark_artifact_purged(_normalized(db_connection, docx_doc, "brief.pdf"))
    assert _preview(db_connection, docx_doc) is None
    with pytest.raises(HTTPException) as exc:
        asyncio.run(documents.get_document_pdf(docx_doc, member("tenant-a", "admin")))
    assert exc.value.status_code == 404


@pytest.mark.parametrize(
    "purged, pointed_at_purged, served",
    [("newer", False, "older.pdf"), ("older", True, "newer.pdf")],
    ids=["newest-purged-uses-older-live", "pointer-purged-uses-newer-live"],
)
def test_preview_skips_a_purged_pdf(docx_doc, db_connection, purged, pointed_at_purged, served):
    ids = {age: _normalized(db_connection, docx_doc, f"{age}.pdf") for age in ("older", "newer")}
    db_connection.mark_artifact_purged(ids[purged])
    if pointed_at_purged:
        seed_document(db_connection, docx_doc, **DOCX, normalized_artifact_id=ids[purged])
    uri, name = _preview(db_connection, docx_doc)
    assert uri.endswith(served)
    assert name.endswith(".pdf")
