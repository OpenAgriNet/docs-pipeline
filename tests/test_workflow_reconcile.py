"""Orphan reconcile must not mark Temporal-missing documents failed.

Bulk reconcile must also account for every outcome it produces: a run that
advanced documents cannot report itself as a no-op (#133).
"""

from __future__ import annotations

import ast
import asyncio
import inspect

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


class _BrokenHandle:
    async def query(self, _name):
        raise RuntimeError("query deserialization blew up")


class _StageHandle:
    def __init__(self, stage):
        self._stage = stage

    async def query(self, _name):
        return {"stage": self._stage}


class _FakeTemporal:
    def __init__(self, handle):
        self._handle = handle

    def get_workflow_handle(self, _workflow_id):
        return self._handle


class _RoutingTemporal:
    """Hands back a different handle per workflow id."""

    def __init__(self, handles):
        self._handles = handles

    def get_workflow_handle(self, workflow_id):
        return self._handles[workflow_id]


def _patch_temporal(monkeypatch, handle):
    client = _FakeTemporal(handle)

    async def _get_client():
        return client

    monkeypatch.setattr(temporal_client, "get_client", _get_client)


def _patch_temporal_router(monkeypatch, handles):
    client = _RoutingTemporal(handles)

    async def _get_client():
        return client

    monkeypatch.setattr(temporal_client, "get_client", _get_client)


def _patch_temporal_outage(monkeypatch):
    async def _get_client():
        raise RuntimeError("Could not connect to Temporal at temporal:7233: connection refused")

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


@pytest.mark.unit
def test_reconcile_temporal_unreachable_is_a_skip_not_an_error(db_connection, monkeypatch):
    """An outage is not a fault in this document, so it must not read as one."""
    workflow_id = "wf-temporal-outage"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-outage",
        filename="outage.pdf",
        filepath="/tmp/outage.pdf",
        stage="chunking",
    )
    _patch_temporal_outage(monkeypatch)

    result = _run(
        workflow_runtime.reconcile_single_document(db_connection.get_document(workflow_id))
    )

    assert result["action"] == "temporal_unavailable"
    assert result["reason"] == "temporal_unreachable"
    assert workflow_runtime.reconcile_outcome_bucket(result["action"]) == "skipped"
    assert db_connection.get_document(workflow_id)["stage"] == "chunking"


@pytest.mark.api
def test_bulk_reconcile_counts_materialized_reconcile_as_updated(db_connection, monkeypatch):
    """A document advanced from its own pages/chunks is an update, not a no-op."""
    workflow_id = "wf-bulk-materialized"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-bulk-materialized",
        filename="stalled.pdf",
        filepath="/tmp/stalled.pdf",
        stage="chunking",
    )
    db_connection.save_pages(workflow_id, [{"page_number": 1, "original_markdown": "page one"}])
    db_connection.save_chunks(
        workflow_id,
        [{"chunk_number": 1, "original_text": "chunk one", "token_count": 2, "page_start": 1, "page_end": 1}],
    )

    async def _never_called():
        raise AssertionError("materialized reconcile must not need Temporal")

    monkeypatch.setattr(temporal_client, "get_client", _never_called)

    result = _run(action_routes.reconcile_document_states(local_bypass_user()))

    detail = next(d for d in result["details"] if d["workflow_id"] == workflow_id)
    assert detail["action"] == "materialized_state_reconciled"
    assert db_connection.get_document(workflow_id)["stage"] == "chunk_review"
    assert result["checked"] == 1
    assert result["updated"] == 1
    assert result["still_running"] == 0
    assert result["skipped"] == 0
    assert result["errors"] == 0


@pytest.mark.api
def test_bulk_reconcile_counts_stage_sync_as_updated(db_connection, monkeypatch):
    """Copying Temporal's stage into SQLite is also an update."""
    workflow_id = "wf-bulk-stage-sync"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-bulk-stage-sync",
        filename="live.pdf",
        filepath="/tmp/live.pdf",
        stage="ocr_review",
    )
    _patch_temporal(monkeypatch, _StageHandle("translation_review"))

    result = _run(action_routes.reconcile_document_states(local_bypass_user()))

    detail = next(d for d in result["details"] if d["workflow_id"] == workflow_id)
    assert detail["action"] == "stage_synced"
    assert detail["from"] == "ocr_review"
    assert detail["to"] == "translation_review"
    assert db_connection.get_document(workflow_id)["stage"] == "translation_review"
    assert result["checked"] == 1
    assert result["updated"] == 1
    assert result["errors"] == 0


@pytest.mark.api
def test_bulk_reconcile_buckets_account_for_every_checked_document(db_connection, monkeypatch):
    """The counters must describe the run: they always sum to `checked`."""
    fixtures = [
        ("wf-mix-materialized", "chunking"),
        ("wf-mix-orphan", "chunking"),
        ("wf-mix-running", "chunking"),
        ("wf-mix-broken", "chunking"),
    ]
    for workflow_id, stage in fixtures:
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id=f"doc-{workflow_id}",
            filename=f"{workflow_id}.pdf",
            filepath=f"/tmp/{workflow_id}.pdf",
            stage=stage,
        )

    # Only this one has materialized content to be reconciled forward.
    db_connection.save_pages("wf-mix-materialized", [{"page_number": 1, "original_markdown": "p"}])
    db_connection.save_chunks(
        "wf-mix-materialized",
        [{"chunk_number": 1, "original_text": "c", "token_count": 1, "page_start": 1, "page_end": 1}],
    )

    _patch_temporal_router(
        monkeypatch,
        {
            "wf-mix-orphan": _MissingHandle(),
            "wf-mix-running": _StageHandle("chunking"),
            "wf-mix-broken": _BrokenHandle(),
        },
    )

    result = _run(action_routes.reconcile_document_states(local_bypass_user()))

    actions = {d["workflow_id"]: d["action"] for d in result["details"]}
    assert actions["wf-mix-materialized"] == "materialized_state_reconciled"
    assert actions["wf-mix-orphan"] == "temporal_not_found"
    assert actions["wf-mix-running"] == "no_change"
    assert actions["wf-mix-broken"] == "error"

    assert result["checked"] == 4
    assert result["updated"] == 1
    assert result["still_running"] == 1
    assert result["skipped"] == 1
    assert result["errors"] == 1
    assert (
        result["updated"] + result["still_running"] + result["skipped"] + result["errors"]
        == result["checked"]
    )


@pytest.mark.unit
def test_every_reconcile_action_has_a_summary_bucket():
    """Guards the regression that caused #133: an action nobody counted."""
    source = inspect.getsource(workflow_runtime.reconcile_single_document)
    tree = ast.parse(source.lstrip())

    returned_actions = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "action"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                returned_actions.add(value.value)

    assert returned_actions, "failed to parse any reconcile actions out of the source"
    unbucketed = returned_actions - set(workflow_runtime.RECONCILE_OUTCOME_BUCKETS)
    assert not unbucketed, f"reconcile actions missing from the summary buckets: {sorted(unbucketed)}"

    # The stage machine no longer has a failure verdict; nothing may reintroduce
    # one through the summary layer.
    assert "marked_failed" not in returned_actions
    assert "marked_failed" not in workflow_runtime.RECONCILE_OUTCOME_BUCKETS
    assert "marked_failed" not in inspect.getsource(action_routes.reconcile_document_states)

    # An action nobody anticipated is still counted, never dropped.
    assert workflow_runtime.reconcile_outcome_bucket("something_new") == "errors"
    assert workflow_runtime.reconcile_outcome_bucket(None) == "errors"


@pytest.mark.api
def test_bulk_reconcile_pre_try_failure_does_not_abort_later_documents(db_connection, monkeypatch):
    """A SQLite fault before the Temporal try must count as error and continue."""
    db_connection.upsert_document(
        workflow_id="wf-boom",
        document_id="doc-boom",
        filename="boom.pdf",
        filepath="/tmp/boom.pdf",
        stage="chunking",
    )
    db_connection.upsert_document(
        workflow_id="wf-ok",
        document_id="doc-ok",
        filename="ok.pdf",
        filepath="/tmp/ok.pdf",
        stage="chunking",
    )

    real_reconcile = workflow_runtime.db.reconcile_materialized_state

    def _boom_then_real(workflow_id):
        if workflow_id == "wf-boom":
            raise RuntimeError("sqlite is locked")
        return real_reconcile(workflow_id)

    monkeypatch.setattr(workflow_runtime.db, "reconcile_materialized_state", _boom_then_real)
    _patch_temporal_router(monkeypatch, {"wf-ok": _StageHandle("chunking")})

    result = _run(action_routes.reconcile_document_states(local_bypass_user()))

    actions = {d["workflow_id"]: d["action"] for d in result["details"]}
    assert actions["wf-boom"] == "error"
    assert "sqlite is locked" in (next(d for d in result["details"] if d["workflow_id"] == "wf-boom").get("reason") or "")
    assert actions["wf-ok"] == "no_change"
    assert result["checked"] == 2
    assert result["errors"] == 1
    assert result["still_running"] == 1
    assert result["updated"] == 0
    assert result["skipped"] == 0
    assert (
        result["updated"] + result["still_running"] + result["skipped"] + result["errors"]
        == result["checked"]
    )
