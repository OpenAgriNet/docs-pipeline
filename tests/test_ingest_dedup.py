"""Ingest dedup: shared ``dedup_or_none`` for POST /documents and POST /upload."""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from pipeline.auth.models import local_bypass_user
from pipeline.models import DocumentStage
from pipeline.services import documents as document_service
from pipeline.services import workflow_runtime
from pipeline.storage import minio as minio_storage


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _disable_upload_limiter():
    """These route tests share the process-wide SlowAPI limiter with the suite."""
    from pipeline.rate_limit import limiter

    was = getattr(limiter, "enabled", True)
    limiter.enabled = False
    try:
        yield
    finally:
        limiter.enabled = was


def _upload_path_workflow_id(content: bytes, filename: str, instance: str = "default") -> str:
    file_hash = hashlib.md5(content).hexdigest()
    object_name = f"{instance}/{file_hash}/{filename}"
    minio_path = f"minio://{minio_storage.bucket_name()}/{object_name}"
    return workflow_runtime.tenant_workflow_id(
        workflow_runtime.get_workflow_id(minio_path), instance
    )


def _run(coro):
    return asyncio.run(coro)


def _tracking_temporal():
    """Temporal mock that only answers get_state for workflows that were started."""
    client = AsyncMock()
    live: dict[str, dict] = {}

    def get_workflow_handle(workflow_id: str):
        handle = AsyncMock()
        if workflow_id in live:
            handle.query = AsyncMock(return_value=live[workflow_id])
        else:
            handle.query = AsyncMock(side_effect=Exception("workflow not found"))
        handle.signal = AsyncMock()
        handle.cancel = AsyncMock()
        return handle

    async def start_workflow(*args, **kwargs):
        workflow_id = kwargs.get("id")
        assert workflow_id, "start_workflow requires id="
        live[workflow_id] = {
            "stage": "registered",
            "document_id": "test-doc-id",
            "filename": "test.pdf",
            "page_count": 0,
            "chunk_count": 0,
            "error_message": None,
        }
        return get_workflow_handle(workflow_id)

    client.get_workflow_handle = MagicMock(side_effect=get_workflow_handle)
    client.start_workflow = AsyncMock(side_effect=start_workflow)
    client._live = live
    return client


@pytest.fixture
def tracking_temporal(monkeypatch):
    client = _tracking_temporal()

    async def _get_client():
        return client

    monkeypatch.setattr("pipeline.temporal.client.get_client", _get_client)
    monkeypatch.setattr(
        "pipeline.temporal.client.get_client_or_none",
        AsyncMock(return_value=client),
    )
    # Match how TestClient fixtures inject the client
    import pipeline.temporal.client as temporal_client

    temporal_client._client = client
    return client


@pytest.mark.api
@pytest.mark.unit
def test_dedup_or_none_hit_sets_duplicate_true(db_connection, tracking_temporal):
    wf = "doc-dedup-hit"
    tracking_temporal._live[wf] = {
        "stage": "registered",
        "page_count": 0,
        "chunk_count": 0,
        "error_message": None,
    }
    db_connection.upsert_document(
        workflow_id=wf,
        document_id="fp1",
        canonical_document_id="fp1",
        filename="a.pdf",
        source_filename="a.pdf",
        source_file_fingerprint="fp1",
        filepath="/tmp/a.pdf",
        stage="registered",
        instance="default",
    )
    summary, wid = _run(
        document_service.dedup_or_none(
            local_bypass_user(),
            wf,
            document_id="fp1",
            canonical_document_id="fp1",
            filename="a.pdf",
            source_filename="a.pdf",
            source_file_fingerprint="fp1",
        )
    )
    assert wid == wf
    assert summary is not None
    assert summary.duplicate is True
    assert summary.workflow_id == wf
    assert summary.stage == DocumentStage.REGISTERED


@pytest.mark.api
@pytest.mark.unit
def test_dedup_or_none_miss_without_sqlite_keeps_stable_id(db_connection, tracking_temporal):
    summary, wid = _run(
        document_service.dedup_or_none(
            local_bypass_user(),
            "doc-missing",
            document_id="fp",
            canonical_document_id="fp",
            filename="a.pdf",
            source_filename="a.pdf",
            source_file_fingerprint="fp",
        )
    )
    assert summary is None
    assert wid == "doc-missing"


@pytest.mark.api
@pytest.mark.unit
def test_dedup_or_none_orphan_temporal_allocates_rerun(db_connection, tracking_temporal):
    """SQLite purged but Temporal still answers → new run id."""
    wf = "doc-orphan"
    tracking_temporal._live[wf] = {"stage": "registered", "page_count": 0, "chunk_count": 0}
    summary, wid = _run(
        document_service.dedup_or_none(
            local_bypass_user(),
            wf,
            document_id="fp1",
            canonical_document_id="fp1",
            filename="a.pdf",
            source_filename="a.pdf",
            source_file_fingerprint="fp1",
        )
    )
    assert summary is None
    assert wid.startswith("doc-orphan-rerun-")


@pytest.mark.api
@pytest.mark.unit
def test_dedup_or_none_force_skips_live_hit(db_connection, tracking_temporal):
    wf = "doc-force"
    tracking_temporal._live[wf] = {"stage": "registered", "page_count": 0, "chunk_count": 0}
    db_connection.upsert_document(
        workflow_id=wf,
        document_id="fp1",
        filename="a.pdf",
        filepath="/tmp/a.pdf",
        stage="registered",
        instance="default",
    )
    summary, wid = _run(
        document_service.dedup_or_none(
            local_bypass_user(),
            wf,
            document_id="fp1",
            canonical_document_id="fp1",
            filename="a.pdf",
            source_filename="a.pdf",
            source_file_fingerprint="fp1",
            force=True,
        )
    )
    assert summary is None
    assert wid.startswith("doc-force-rerun-")
    tracking_temporal.get_workflow_handle.assert_not_called()


@pytest.mark.api
@pytest.mark.unit
def test_upload_second_identical_file_returns_duplicate(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    files = {"file": ("sample.pdf", sample_pdf_content, "application/pdf")}
    first = test_client.post("/upload", files=files)
    assert first.status_code == 200, first.text
    body1 = first.json()
    assert body1.get("duplicate") is False
    assert tracking_temporal.start_workflow.call_count >= 1
    starts_after_first = tracking_temporal.start_workflow.call_count

    second = test_client.post("/upload", files=files)
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["duplicate"] is True
    assert body2["workflow_id"] == body1["workflow_id"]
    assert tracking_temporal.start_workflow.call_count == starts_after_first


@pytest.mark.api
@pytest.mark.unit
def test_documents_register_second_identical_path_returns_duplicate(
    test_client, temp_pdf_file, monkeypatch, db_connection, tracking_temporal
):
    monkeypatch.setattr(
        "pipeline.services.source_files.ALLOWED_FILE_PATHS",
        [str(temp_pdf_file.parent)],
    )
    payload = {"filepath": str(temp_pdf_file)}

    first = test_client.post("/documents", json=payload)
    assert first.status_code == 200, first.text
    body1 = first.json()
    assert body1.get("duplicate") is False
    starts_after_first = tracking_temporal.start_workflow.call_count

    second = test_client.post("/documents", json=payload)
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["duplicate"] is True
    assert body2["workflow_id"] == body1["workflow_id"]
    assert tracking_temporal.start_workflow.call_count == starts_after_first


@pytest.mark.api
@pytest.mark.unit
def test_upload_same_bytes_different_filename_returns_existing(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    first = test_client.post(
        "/upload",
        files={"file": ("report_final.pdf", sample_pdf_content, "application/pdf")},
    )
    assert first.status_code == 200, first.text
    body1 = first.json()
    assert body1.get("duplicate") is False
    starts_after_first = tracking_temporal.start_workflow.call_count
    puts_after_first = mock_minio_client.put_object.call_count

    second = test_client.post(
        "/upload",
        files={"file": ("Report Final.pdf", sample_pdf_content, "application/pdf")},
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["duplicate"] is True
    assert body2["workflow_id"] == body1["workflow_id"]
    assert tracking_temporal.start_workflow.call_count == starts_after_first
    assert mock_minio_client.put_object.call_count == puts_after_first


@pytest.mark.api
@pytest.mark.unit
def test_fingerprint_hit_without_temporal_reuses_sqlite_row(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    fp = hashlib.md5(sample_pdf_content).hexdigest()
    db_connection.upsert_document(
        workflow_id="rebuild-doc-existing",
        document_id=fp,
        canonical_document_id=fp,
        filename="report_final.pdf",
        source_filename="report_final.pdf",
        source_file_fingerprint=fp,
        filepath="/data/rebuild/report_final.pdf",
        stage="completed",
        instance="default",
    )
    starts_before = tracking_temporal.start_workflow.call_count

    resp = test_client.post(
        "/upload",
        files={"file": ("Report Final.pdf", sample_pdf_content, "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicate"] is True
    assert body["workflow_id"] == "rebuild-doc-existing"
    assert body["stage"] == "completed"
    assert tracking_temporal.start_workflow.call_count == starts_before
    assert mock_minio_client.put_object.call_count == 0


@pytest.mark.api
@pytest.mark.unit
def test_fingerprint_prefers_completed_over_failed_sibling(db_connection, tracking_temporal):
    fp = "shared-content-hash"
    db_connection.upsert_document(
        workflow_id="doc-failed-sibling",
        document_id=fp,
        canonical_document_id=fp,
        filename="Other Name.pdf",
        source_file_fingerprint=fp,
        filepath="/tmp/other.pdf",
        stage="failed",
        instance="default",
    )
    db_connection.upsert_document(
        workflow_id="rebuild-doc-completed",
        document_id=fp,
        canonical_document_id=fp,
        filename="slug_name.pdf",
        source_file_fingerprint=fp,
        filepath="/tmp/slug.pdf",
        stage="completed",
        instance="default",
    )
    hit = db_connection.find_live_document_by_fingerprint("default", fp)
    assert hit["workflow_id"] == "rebuild-doc-completed"

    summary, wid = _run(
        document_service.dedup_or_none(
            local_bypass_user(),
            "doc-new-path-id",
            document_id=fp,
            canonical_document_id=fp,
            filename="Other Name.pdf",
            source_filename="Other Name.pdf",
            source_file_fingerprint=fp,
            instance="default",
        )
    )
    assert summary is not None
    assert summary.duplicate is True
    assert wid == "rebuild-doc-completed"


@pytest.mark.api
@pytest.mark.unit
def test_soft_deleted_fingerprint_is_not_a_duplicate(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    fp = hashlib.md5(sample_pdf_content).hexdigest()
    db_connection.upsert_document(
        workflow_id="doc-disabled",
        document_id=fp,
        canonical_document_id=fp,
        filename="gone.pdf",
        source_file_fingerprint=fp,
        filepath="/tmp/gone.pdf",
        stage="completed",
        instance="default",
    )
    db_connection.set_document_disabled("doc-disabled", True)

    resp = test_client.post(
        "/upload",
        files={"file": ("gone.pdf", sample_pdf_content, "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("duplicate") is False
    assert body["workflow_id"] != "doc-disabled"
    assert tracking_temporal.start_workflow.call_count >= 1


@pytest.mark.api
@pytest.mark.unit
def test_dedup_or_none_disabled_same_path_live_temporal_starts_fresh(
    db_connection, tracking_temporal
):
    wf = "doc-disabled-path-live"
    tracking_temporal._live[wf] = {
        "stage": "completed",
        "page_count": 1,
        "chunk_count": 1,
        "error_message": None,
    }
    db_connection.upsert_document(
        workflow_id=wf,
        document_id="fp-disabled-live",
        canonical_document_id="fp-disabled-live",
        filename="same.pdf",
        source_file_fingerprint="fp-disabled-live",
        filepath="/tmp/same.pdf",
        stage="completed",
        instance="default",
    )
    db_connection.set_document_disabled(wf, True)

    summary, wid = _run(
        document_service.dedup_or_none(
            local_bypass_user(),
            wf,
            document_id="fp-disabled-live",
            canonical_document_id="fp-disabled-live",
            filename="same.pdf",
            source_filename="same.pdf",
            source_file_fingerprint="fp-disabled-live",
            instance="default",
        )
    )
    assert summary is None
    assert wid.startswith(f"{wf}-rerun-")
    assert wid != wf


@pytest.mark.api
@pytest.mark.unit
def test_dedup_or_none_disabled_same_path_closed_temporal_starts_fresh(
    db_connection, tracking_temporal
):
    wf = "doc-disabled-path-closed"
    db_connection.upsert_document(
        workflow_id=wf,
        document_id="fp-disabled-closed",
        canonical_document_id="fp-disabled-closed",
        filename="same.pdf",
        source_file_fingerprint="fp-disabled-closed",
        filepath="/tmp/same.pdf",
        stage="completed",
        instance="default",
    )
    db_connection.set_document_disabled(wf, True)

    summary, wid = _run(
        document_service.dedup_or_none(
            local_bypass_user(),
            wf,
            document_id="fp-disabled-closed",
            canonical_document_id="fp-disabled-closed",
            filename="same.pdf",
            source_filename="same.pdf",
            source_file_fingerprint="fp-disabled-closed",
            instance="default",
        )
    )
    assert summary is None
    assert wid.startswith(f"{wf}-rerun-")
    assert wid != wf


@pytest.mark.api
@pytest.mark.unit
def test_upload_disabled_same_path_live_temporal_starts_fresh(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    filename = "gone-same-path-live.pdf"
    wf = _upload_path_workflow_id(sample_pdf_content, filename)
    fp = hashlib.md5(sample_pdf_content).hexdigest()
    tracking_temporal._live[wf] = {
        "stage": "completed",
        "page_count": 1,
        "chunk_count": 1,
        "error_message": None,
    }
    db_connection.upsert_document(
        workflow_id=wf,
        document_id=fp,
        canonical_document_id=fp,
        filename=filename,
        source_file_fingerprint=fp,
        filepath=f"minio://documents/default/{fp}/{filename}",
        stage="completed",
        instance="default",
    )
    db_connection.set_document_disabled(wf, True)

    resp = test_client.post(
        "/upload",
        files={"file": (filename, sample_pdf_content, "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("duplicate") is False
    assert body["workflow_id"] != wf
    assert body["workflow_id"].startswith(f"{wf}-rerun-")
    assert tracking_temporal.start_workflow.call_count >= 1


@pytest.mark.api
@pytest.mark.unit
def test_upload_disabled_same_path_closed_temporal_starts_fresh(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    filename = "gone-same-path-closed.pdf"
    wf = _upload_path_workflow_id(sample_pdf_content, filename)
    fp = hashlib.md5(sample_pdf_content).hexdigest()
    db_connection.upsert_document(
        workflow_id=wf,
        document_id=fp,
        canonical_document_id=fp,
        filename=filename,
        source_file_fingerprint=fp,
        filepath=f"minio://documents/default/{fp}/{filename}",
        stage="completed",
        instance="default",
    )
    db_connection.set_document_disabled(wf, True)

    resp = test_client.post(
        "/upload",
        files={"file": (filename, sample_pdf_content, "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("duplicate") is False
    assert body["workflow_id"] != wf
    assert body["workflow_id"].startswith(f"{wf}-rerun-")
    assert tracking_temporal.start_workflow.call_count >= 1


@pytest.mark.api
@pytest.mark.unit
def test_same_fingerprint_other_tenant_is_not_a_duplicate(
    test_client, mock_minio_client, sample_pdf_content, db_connection, tracking_temporal
):
    fp = hashlib.md5(sample_pdf_content).hexdigest()
    db_connection.upsert_document(
        workflow_id="doc-tenant-a",
        document_id=fp,
        canonical_document_id=fp,
        filename="shared.pdf",
        source_file_fingerprint=fp,
        filepath="/tmp/a.pdf",
        stage="completed",
        instance="tenant-a",
    )
    resp = test_client.post(
        "/upload",
        params={"instance": "tenant-b"},
        files={"file": ("shared.pdf", sample_pdf_content, "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("duplicate") is False
    assert body["workflow_id"] != "doc-tenant-a"
    assert body.get("instance") == "tenant-b"


@pytest.mark.api
@pytest.mark.unit
def test_documents_register_same_bytes_different_path_returns_existing(
    test_client, temp_pdf_file, monkeypatch, db_connection, tracking_temporal
):
    other = temp_pdf_file.parent / "other-name.pdf"
    other.write_bytes(temp_pdf_file.read_bytes())
    monkeypatch.setattr(
        "pipeline.services.source_files.ALLOWED_FILE_PATHS",
        [str(temp_pdf_file.parent)],
    )

    first = test_client.post("/documents", json={"filepath": str(temp_pdf_file)})
    assert first.status_code == 200, first.text
    body1 = first.json()
    assert body1.get("duplicate") is False
    starts_after_first = tracking_temporal.start_workflow.call_count

    second = test_client.post("/documents", json={"filepath": str(other)})
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["duplicate"] is True
    assert body2["workflow_id"] == body1["workflow_id"]
    assert tracking_temporal.start_workflow.call_count == starts_after_first
