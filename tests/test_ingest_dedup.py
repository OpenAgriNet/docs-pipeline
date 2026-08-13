"""Ingest dedup: shared ``_dedup_or_none`` for POST /documents and POST /upload."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import pipeline.api as api
from pipeline.auth.models import local_bypass_user
from pipeline.models import DocumentStage


def _run(coro):
    return asyncio.run(coro)


def _temporal_tracking_client():
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
    client = _temporal_tracking_client()
    monkeypatch.setattr(api, "temporal_client", client)
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
    api.db.upsert_document(
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
        api._dedup_or_none(
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
    tracking_temporal.start_workflow.assert_not_called()


@pytest.mark.api
@pytest.mark.unit
def test_dedup_or_none_miss_without_sqlite_keeps_stable_id(db_connection, tracking_temporal):
    summary, wid = _run(
        api._dedup_or_none(
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
        api._dedup_or_none(
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
    api.db.upsert_document(
        workflow_id=wf,
        document_id="fp1",
        filename="a.pdf",
        filepath="/tmp/a.pdf",
        stage="registered",
        instance="default",
    )
    summary, wid = _run(
        api._dedup_or_none(
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
    assert first.status_code == 200
    body1 = first.json()
    assert body1.get("duplicate") is False
    assert tracking_temporal.start_workflow.call_count >= 1
    starts_after_first = tracking_temporal.start_workflow.call_count

    second = test_client.post("/upload", files=files)
    assert second.status_code == 200
    body2 = second.json()
    assert body2["duplicate"] is True
    assert body2["workflow_id"] == body1["workflow_id"]
    assert tracking_temporal.start_workflow.call_count == starts_after_first


@pytest.mark.api
@pytest.mark.unit
def test_documents_register_second_identical_path_returns_duplicate(
    test_client, temp_pdf_file, monkeypatch, db_connection, tracking_temporal
):
    monkeypatch.setattr(api, "ALLOWED_FILE_PATHS", [str(temp_pdf_file.parent)])
    payload = {"filepath": str(temp_pdf_file)}

    first = test_client.post("/documents", json=payload)
    assert first.status_code == 200
    body1 = first.json()
    assert body1.get("duplicate") is False
    starts_after_first = tracking_temporal.start_workflow.call_count

    second = test_client.post("/documents", json=payload)
    assert second.status_code == 200
    body2 = second.json()
    assert body2["duplicate"] is True
    assert body2["workflow_id"] == body1["workflow_id"]
    assert tracking_temporal.start_workflow.call_count == starts_after_first
