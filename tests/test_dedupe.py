"""Tests for content-fingerprint upload deduplication."""

from __future__ import annotations

import hashlib
from io import BytesIO
from unittest.mock import AsyncMock

import pytest


PDF_BYTES = b"%PDF-1.4\n%demo content for fingerprint tests\n"


@pytest.mark.api
@pytest.mark.unit
def test_get_document_by_fingerprint_scopes_instance(db_connection):
    fp = hashlib.md5(PDF_BYTES).hexdigest()
    db_connection.upsert_document(
        workflow_id="wf-tenant-a",
        document_id=fp,
        filename="a.pdf",
        filepath="minio://documents/a.pdf",
        stage="completed",
        source_file_fingerprint=fp,
        instance="tenant-a",
    )
    db_connection.upsert_document(
        workflow_id="wf-tenant-b",
        document_id=fp,
        filename="b.pdf",
        filepath="minio://documents/b.pdf",
        stage="completed",
        source_file_fingerprint=fp,
        instance="tenant-b",
    )

    hit_a = db_connection.get_document_by_fingerprint(fp, instance="tenant-a")
    hit_b = db_connection.get_document_by_fingerprint(fp, instance="tenant-b")
    assert hit_a["workflow_id"] == "wf-tenant-a"
    assert hit_b["workflow_id"] == "wf-tenant-b"
    assert db_connection.get_document_by_fingerprint(fp, instance="tenant-c") is None


@pytest.mark.api
@pytest.mark.unit
def test_get_document_by_fingerprint_skips_disabled(db_connection):
    fp = "abc123fingerprint"
    db_connection.upsert_document(
        workflow_id="wf-disabled",
        document_id=fp,
        filename="gone.pdf",
        filepath="minio://documents/gone.pdf",
        stage="completed",
        source_file_fingerprint=fp,
        instance="default",
    )
    db_connection.set_document_disabled("wf-disabled", True)
    assert db_connection.get_document_by_fingerprint(fp, instance="default") is None
    assert (
        db_connection.get_document_by_fingerprint(
            fp, instance="default", include_disabled=True
        )["workflow_id"]
        == "wf-disabled"
    )


@pytest.mark.api
@pytest.mark.unit
def test_upload_deduplicates_same_content_different_filename(test_client, db_connection):
    from pipeline import api

    fp = hashlib.md5(PDF_BYTES).hexdigest()
    db_connection.upsert_document(
        workflow_id="wf-existing",
        document_id=fp,
        filename="original.pdf",
        filepath=f"minio://documents/{fp}/original.pdf",
        stage="completed",
        source_file_fingerprint=fp,
        instance="default",
        page_count=3,
        chunk_count=5,
    )

    api.temporal_client.start_workflow = AsyncMock()
    api.minio_client.put_object.reset_mock()

    response = test_client.post(
        "/upload",
        files={"file": ("renamed-copy.pdf", BytesIO(PDF_BYTES), "application/pdf")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["deduplicated"] is True
    assert body["workflow_id"] == "wf-existing"
    assert body["source_file_fingerprint"] == fp
    api.minio_client.put_object.assert_not_called()
    api.temporal_client.start_workflow.assert_not_called()


@pytest.mark.api
@pytest.mark.unit
def test_upload_force_new_bypasses_fingerprint_dedupe(test_client, db_connection):
    from pipeline import api

    fp = hashlib.md5(PDF_BYTES).hexdigest()
    db_connection.upsert_document(
        workflow_id="wf-existing",
        document_id=fp,
        filename="original.pdf",
        filepath=f"minio://documents/{fp}/original.pdf",
        stage="completed",
        source_file_fingerprint=fp,
        instance="default",
    )

    api.temporal_client.start_workflow = AsyncMock(return_value=api.temporal_client.get_workflow_handle("x"))

    response = test_client.post(
        "/upload?force_new=true",
        files={"file": ("again.pdf", BytesIO(PDF_BYTES), "application/pdf")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deduplicated"] is False
    assert body["workflow_id"] != "wf-existing"
    assert "-rerun-" in body["workflow_id"]
    api.temporal_client.start_workflow.assert_awaited()
