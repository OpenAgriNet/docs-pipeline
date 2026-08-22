"""Orphan reconcile must not mark Temporal-missing documents failed."""

from __future__ import annotations

import asyncio

import pytest

from pipeline.auth.models import local_bypass_user
from pipeline.routers import documents_actions as action_routes
from pipeline.services import workflow_runtime
from pipeline.temporal import client as temporal_client


def _run(coro):
    return asyncio.run(coro)


class _MissingHandle:
    async def query(self, _name):
        raise RuntimeError("workflow not found")


class _TimeoutHandle:
    async def query(self, _name):
        raise asyncio.TimeoutError()


class _FakeTemporal:
    def __init__(self, handle):
        self._handle = handle

    def get_workflow_handle(self, _workflow_id):
        return self._handle


def _patch_temporal(monkeypatch, handle):
    client = _FakeTemporal(handle)

    async def _get_client():
        return client

    monkeypatch.setattr(temporal_client, "get_client", _get_client)


@pytest.mark.unit
def test_reconcile_temporal_not_found_leaves_sqlite_stage(db_connection, monkeypatch):
    workflow_id = "wf-orphan-ocr"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-orphan",
        filename="stuck.pdf",
        filepath="/tmp/stuck.pdf",
        stage="ocr_processing",
    )
    _patch_temporal(monkeypatch, _MissingHandle())

    result = _run(
        workflow_runtime.reconcile_single_document(db_connection.get_document(workflow_id))
    )

    assert result["action"] == "temporal_not_found"
    assert result["stage"] == "ocr_processing"
    refreshed = db_connection.get_document(workflow_id)
    assert refreshed["stage"] == "ocr_processing"
    assert not refreshed.get("error_message")


@pytest.mark.unit
def test_reconcile_temporal_timeout_leaves_sqlite_stage(db_connection, monkeypatch):
    workflow_id = "wf-orphan-timeout"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-timeout",
        filename="slow.pdf",
        filepath="/tmp/slow.pdf",
        stage="translation_processing",
    )
    _patch_temporal(monkeypatch, _TimeoutHandle())

    result = _run(
        workflow_runtime.reconcile_single_document(db_connection.get_document(workflow_id))
    )

    assert result["action"] == "temporal_unavailable"
    refreshed = db_connection.get_document(workflow_id)
    assert refreshed["stage"] == "translation_processing"


@pytest.mark.api
def test_bulk_reconcile_counts_temporal_not_found_as_skipped(db_connection, monkeypatch):
    db_connection.upsert_document(
        workflow_id="wf-bulk-orphan",
        document_id="doc-bulk-orphan",
        filename="bulk.pdf",
        filepath="/tmp/bulk.pdf",
        stage="chunking",
    )
    _patch_temporal(monkeypatch, _MissingHandle())

    result = _run(action_routes.reconcile_document_states(local_bypass_user()))

    assert result["checked"] >= 1
    assert result["skipped"] >= 1
    assert result["updated"] == 0
    orphan = next(d for d in result["details"] if d["workflow_id"] == "wf-bulk-orphan")
    assert orphan["action"] == "temporal_not_found"
    assert db_connection.get_document("wf-bulk-orphan")["stage"] == "chunking"
