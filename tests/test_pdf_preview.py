"""GET /pdf must stream a real PDF, not the original Office bytes."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from pipeline.routers import documents
from pipeline.services import documents as document_service


def _run(coro):
    return asyncio.run(coro)


def _admin():
    from pipeline.auth.jwt import claims_to_user

    return claims_to_user({"sub": "tadmin", "tenant_roles": {"tenant-a": ["admin"]}})


@pytest.fixture
def docx_doc(db_connection):
    db_connection.upsert_document(
        workflow_id="wf-preview-docx",
        document_id="doc-preview-docx",
        filename="Buffalor Brief.docx",
        filepath="minio://documents/amul/brief.docx",
        stage="ocr_review",
        instance="tenant-a",
    )
    return "wf-preview-docx"


def test_preview_prefers_normalized_pdf_for_office(docx_doc, db_connection):
    db_connection.add_document_artifact(
        docx_doc,
        "normalized_pdf",
        "minio://documents/amul/wf-preview-docx/normalized_pdf/brief.pdf",
        filename="brief.pdf",
        mime_type="application/pdf",
    )
    doc = db_connection.get_document(docx_doc)
    uri, name = document_service.preview_pdf_source(doc)
    assert uri.endswith("brief.pdf")
    assert name.endswith(".pdf")
    assert "brief.docx" not in uri.lower()


def test_preview_falls_back_to_pdf_filepath(db_connection):
    db_connection.upsert_document(
        workflow_id="wf-preview-pdf",
        document_id="doc-preview-pdf",
        filename="note.pdf",
        filepath="minio://documents/amul/note.pdf",
        stage="ocr_review",
        instance="tenant-a",
    )
    uri, name = document_service.preview_pdf_source(db_connection.get_document("wf-preview-pdf"))
    assert uri.endswith("note.pdf")
    assert name.endswith(".pdf")


def test_preview_office_without_conversion_is_unavailable(docx_doc, db_connection):
    assert document_service.preview_pdf_source(db_connection.get_document(docx_doc)) is None


def test_get_document_pdf_streams_normalized_object(docx_doc, db_connection, monkeypatch):
    db_connection.add_document_artifact(
        docx_doc,
        "normalized_pdf",
        "minio://documents/amul/wf-preview-docx/normalized_pdf/brief.pdf",
        filename="brief.pdf",
        mime_type="application/pdf",
    )
    seen = []

    class _Obj:
        def __iter__(self):
            yield b"%PDF-1.4 test"

    def _get_object(bucket, name):
        seen.append((bucket, name))
        return _Obj()

    monkeypatch.setattr(
        "pipeline.routers.documents.minio_storage.get_client",
        lambda: type("C", (), {"get_object": staticmethod(_get_object)})(),
    )
    response = _run(documents.get_document_pdf(docx_doc, _admin()))
    assert response.media_type == "application/pdf"
    assert seen == [("documents", "amul/wf-preview-docx/normalized_pdf/brief.pdf")]


def test_get_document_pdf_404_when_office_has_no_pdf(docx_doc):
    with pytest.raises(HTTPException) as exc:
        _run(documents.get_document_pdf(docx_doc, _admin()))
    assert exc.value.status_code == 404


def test_preview_skips_purged_pointer_uses_older_live(db_connection):
    db_connection.upsert_document(
        workflow_id="wf-preview-gc-older",
        document_id="doc-preview-gc-older",
        filename="notes.docx",
        filepath="minio://documents/tenant-a/notes.docx",
        stage="ocr_review",
        instance="tenant-a",
    )
    db_connection.add_document_artifact(
        "wf-preview-gc-older",
        "normalized_pdf",
        "minio://documents/tenant-a/wf-preview-gc-older/normalized_pdf/older.pdf",
        filename="older.pdf",
        mime_type="application/pdf",
    )
    newer_id = db_connection.add_document_artifact(
        "wf-preview-gc-older",
        "normalized_pdf",
        "minio://documents/tenant-a/wf-preview-gc-older/normalized_pdf/newer.pdf",
        filename="newer.pdf",
        mime_type="application/pdf",
    )
    db_connection.mark_artifact_purged(newer_id)

    uri, name = document_service.preview_pdf_source(
        db_connection.get_document("wf-preview-gc-older")
    )
    assert uri.endswith("older.pdf")
    assert name.endswith(".pdf")


def test_preview_skips_purged_pointer_uses_newer_live(db_connection):
    db_connection.upsert_document(
        workflow_id="wf-preview-gc-newer",
        document_id="doc-preview-gc-newer",
        filename="notes.docx",
        filepath="minio://documents/tenant-a/notes.docx",
        stage="ocr_review",
        instance="tenant-a",
    )
    older_id = db_connection.add_document_artifact(
        "wf-preview-gc-newer",
        "normalized_pdf",
        "minio://documents/tenant-a/wf-preview-gc-newer/normalized_pdf/older.pdf",
        filename="older.pdf",
        mime_type="application/pdf",
    )
    db_connection.add_document_artifact(
        "wf-preview-gc-newer",
        "normalized_pdf",
        "minio://documents/tenant-a/wf-preview-gc-newer/normalized_pdf/newer.pdf",
        filename="newer.pdf",
        mime_type="application/pdf",
    )
    db_connection.mark_artifact_purged(older_id)
    db_connection.upsert_document(
        workflow_id="wf-preview-gc-newer",
        document_id="doc-preview-gc-newer",
        filename="notes.docx",
        filepath="minio://documents/tenant-a/notes.docx",
        stage="ocr_review",
        instance="tenant-a",
        normalized_artifact_id=older_id,
    )

    uri, name = document_service.preview_pdf_source(
        db_connection.get_document("wf-preview-gc-newer")
    )
    assert uri.endswith("newer.pdf")
    assert name.endswith(".pdf")


def test_preview_purged_only_office_is_unavailable(docx_doc, db_connection):
    artifact_id = db_connection.add_document_artifact(
        docx_doc,
        "normalized_pdf",
        "minio://documents/tenant-a/wf-preview-docx/normalized_pdf/brief.pdf",
        filename="brief.pdf",
        mime_type="application/pdf",
    )
    db_connection.mark_artifact_purged(artifact_id)
    assert document_service.preview_pdf_source(db_connection.get_document(docx_doc)) is None


def test_get_document_pdf_404_when_normalized_purged(docx_doc, db_connection):
    artifact_id = db_connection.add_document_artifact(
        docx_doc,
        "normalized_pdf",
        "minio://documents/tenant-a/wf-preview-docx/normalized_pdf/brief.pdf",
        filename="brief.pdf",
        mime_type="application/pdf",
    )
    db_connection.mark_artifact_purged(artifact_id)
    with pytest.raises(HTTPException) as exc:
        _run(documents.get_document_pdf(docx_doc, _admin()))
    assert exc.value.status_code == 404
