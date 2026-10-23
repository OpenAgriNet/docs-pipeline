"""Orphan reconcile must not mark Temporal-missing documents failed.

Bulk reconcile must also account for every outcome it produces: a run that
advanced documents cannot report itself as a no-op (#133).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pipeline.auth.models import local_bypass_user
from pipeline.routers import documents_actions as action_routes
from pipeline.services import workflow_runtime
from pipeline.temporal import client as temporal_client
from tests.helpers import seed_document

WF = "wf-reconcile"
CHUNK = {"chunk_number": 1, "original_text": "chunk one", "token_count": 2, "page_start": 1, "page_end": 1}


class _Handle:
    """A workflow handle whose state query returns ``{"stage": answer}``, or raises ``answer``."""

    def __init__(self, answer):
        self._answer = answer

    async def query(self, _name):
        if isinstance(self._answer, BaseException):
            raise self._answer
        return {"stage": self._answer}


def _temporal(monkeypatch, handles):
    """Serve ``handles`` (one ``_Handle`` for every workflow, or a dict by workflow id) as Temporal.

    An exception instead makes connecting to Temporal itself raise it.
    """

    async def _get_client():
        if isinstance(handles, BaseException):
            raise handles
        return SimpleNamespace(get_workflow_handle=lambda wf: handles[wf] if isinstance(handles, dict) else handles)

    monkeypatch.setattr(temporal_client, "get_client", _get_client)


MISSING = _Handle(RuntimeError("workflow not found"))


def _reconcile_one(db_connection, workflow_id=WF):
    return asyncio.run(workflow_runtime.reconcile_single_document(db_connection.get_document(workflow_id)))


def _reconcile_all():
    """Bulk reconcile; returns the result and each document's action by workflow id."""
    result = asyncio.run(action_routes.reconcile_document_states(local_bypass_user()))
    return result, {d["workflow_id"]: d["action"] for d in result["details"]}


def _buckets(result):
    counts = {k: result[k] for k in ("checked", "updated", "still_running", "skipped", "errors")}
    assert counts["updated"] + counts["still_running"] + counts["skipped"] + counts["errors"] == counts["checked"]
    return counts


@pytest.mark.unit
def test_reconcile_temporal_not_found_leaves_sqlite_stage(db_connection, monkeypatch):
    seed_document(db_connection, WF, stage="ocr_processing")
    _temporal(monkeypatch, MISSING)

    result = _reconcile_one(db_connection)

    assert result["action"] == "temporal_not_found"
    assert result["stage"] == "ocr_processing"
    refreshed = db_connection.get_document(WF)
    assert refreshed["stage"] == "ocr_processing"
    assert not refreshed.get("error_message")


@pytest.mark.unit
def test_reconcile_temporal_timeout_leaves_sqlite_stage(db_connection, monkeypatch):
    seed_document(db_connection, WF, stage="translation_processing")
    _temporal(monkeypatch, _Handle(asyncio.TimeoutError()))

    assert _reconcile_one(db_connection)["action"] == "temporal_unavailable"
    assert db_connection.get_document(WF)["stage"] == "translation_processing"


@pytest.mark.api
def test_bulk_reconcile_counts_temporal_not_found_as_skipped(db_connection, monkeypatch):
    seed_document(db_connection, WF, stage="chunking")
    _temporal(monkeypatch, MISSING)

    result, actions = _reconcile_all()

    assert result["checked"] >= 1
    assert result["skipped"] >= 1
    assert result["updated"] == 0
    assert actions[WF] == "temporal_not_found"
    assert db_connection.get_document(WF)["stage"] == "chunking"


@pytest.mark.unit
def test_reconcile_temporal_unreachable_is_a_skip_not_an_error(db_connection, monkeypatch):
    """An outage is not a fault in this document, so it must not read as one."""
    seed_document(db_connection, WF, stage="chunking")
    _temporal(monkeypatch, RuntimeError("Could not connect to Temporal at temporal:7233: connection refused"))

    result = _reconcile_one(db_connection)

    assert result["action"] == "temporal_unavailable"
    assert result["reason"] == "temporal_unreachable"
    assert workflow_runtime.reconcile_outcome_bucket(result["action"]) == "skipped"
    assert db_connection.get_document(WF)["stage"] == "chunking"


@pytest.mark.api
def test_bulk_reconcile_counts_materialized_reconcile_as_updated(db_connection, monkeypatch):
    """A document advanced from its own pages/chunks is an update, not a no-op."""
    seed_document(db_connection, WF, stage="chunking", pages=["page one"], chunks=[CHUNK])
    _temporal(monkeypatch, AssertionError("materialized reconcile must not need Temporal"))

    result, actions = _reconcile_all()

    assert actions[WF] == "materialized_state_reconciled"
    assert db_connection.get_document(WF)["stage"] == "chunk_review"
    assert _buckets(result) == {"checked": 1, "updated": 1, "still_running": 0, "skipped": 0, "errors": 0}


@pytest.mark.api
def test_bulk_reconcile_counts_stage_sync_as_updated(db_connection, monkeypatch):
    """Copying Temporal's stage into SQLite is also an update."""
    seed_document(db_connection, WF, stage="ocr_review")
    _temporal(monkeypatch, _Handle("translation_review"))

    result, _ = _reconcile_all()

    detail = next(d for d in result["details"] if d["workflow_id"] == WF)
    assert (detail["action"], detail["from"], detail["to"]) == ("stage_synced", "ocr_review", "translation_review")
    assert db_connection.get_document(WF)["stage"] == "translation_review"
    assert result["checked"] == 1
    assert result["updated"] == 1
    assert result["errors"] == 0


@pytest.mark.api
def test_bulk_reconcile_buckets_account_for_every_checked_document(db_connection, monkeypatch):
    """The counters must describe the run: they always sum to `checked`."""
    for wf in ("wf-mix-materialized", "wf-mix-orphan", "wf-mix-running", "wf-mix-broken"):
        seed_document(db_connection, wf, stage="chunking")
    # Only this one has materialized content to be reconciled forward.
    db_connection.save_pages("wf-mix-materialized", [{"page_number": 1, "original_markdown": "p"}])
    db_connection.save_chunks("wf-mix-materialized", [{**CHUNK, "original_text": "c", "token_count": 1}])
    _temporal(monkeypatch, {
        "wf-mix-orphan": MISSING,
        "wf-mix-running": _Handle("chunking"),
        "wf-mix-broken": _Handle(RuntimeError("query deserialization blew up")),
    })

    result, actions = _reconcile_all()

    assert actions == {
        "wf-mix-materialized": "materialized_state_reconciled",
        "wf-mix-orphan": "temporal_not_found",
        "wf-mix-running": "no_change",
        "wf-mix-broken": "error",
    }
    assert _buckets(result) == {"checked": 4, "updated": 1, "still_running": 1, "skipped": 1, "errors": 1}


@pytest.mark.unit
def test_every_reconcile_action_has_a_summary_bucket():
    """Guards the regression that caused #133: an action nobody anticipated is still counted, never dropped."""
    assert workflow_runtime.reconcile_outcome_bucket("something_new") == "errors"
    assert workflow_runtime.reconcile_outcome_bucket(None) == "errors"


@pytest.mark.api
def test_bulk_reconcile_pre_try_failure_does_not_abort_later_documents(db_connection, monkeypatch):
    """A SQLite fault before the Temporal try must count as error and continue."""
    seed_document(db_connection, "wf-boom", stage="chunking")
    seed_document(db_connection, "wf-ok", stage="chunking")
    real_reconcile = workflow_runtime.db.reconcile_materialized_state

    def _boom_then_real(workflow_id):
        if workflow_id == "wf-boom":
            raise RuntimeError("sqlite is locked")
        return real_reconcile(workflow_id)

    monkeypatch.setattr(workflow_runtime.db, "reconcile_materialized_state", _boom_then_real)
    _temporal(monkeypatch, {"wf-ok": _Handle("chunking")})

    result, actions = _reconcile_all()

    assert actions["wf-boom"] == "error"
    boom = next(d for d in result["details"] if d["workflow_id"] == "wf-boom")
    assert "sqlite is locked" in (boom.get("reason") or "")
    assert actions["wf-ok"] == "no_change"
    assert _buckets(result) == {"checked": 2, "updated": 0, "still_running": 1, "skipped": 0, "errors": 1}


@pytest.mark.unit
@pytest.mark.parametrize(
    "stage, chunk_count, job_status, updated, final_stage",
    [
        pytest.param("chunking", 0, "running", True, "chunking", id="chunk_review_not_promoted_while_job_running"),
        pytest.param("chunking", 0, "failed", True, "chunk_review", id="chunk_review_promoted_when_job_not_running"),
        pytest.param("chunking", 1, "running", False, "chunking", id="running_job_with_matching_counts_is_noop"),
        pytest.param(
            "ocr_processing", 0, "running", True, "ocr_processing", id="ocr_review_not_promoted_while_job_running"
        ),
        pytest.param("ocr_processing", 0, None, True, "ocr_review", id="ocr_review_promoted_without_running_job"),
    ],
)
def test_reconcile_materialized_state(db_connection, stage, chunk_count, job_status, updated, final_stage):
    """Saved pages/chunks advance a stalled stage, never one a running job is still streaming into."""
    chunking = stage == "chunking"
    seed_document(
        db_connection, WF, stage=stage, page_count=1 if chunking else 0, chunk_count=chunk_count,
        pages=["page one"], chunks=[CHUNK] if chunking else (),
    )
    if job_status:
        db_connection.create_document_job(workflow_id=WF, job_type="pipeline", status=job_status, current_stage=stage)

    result = db_connection.reconcile_materialized_state(WF)
    doc = db_connection.get_document(WF)

    assert result["updated"] is updated
    assert doc["stage"] == final_stage
    if job_status == "running" and updated:
        # The counts still catch up while the stage waits for the job.
        assert result["stage"] == final_stage
        assert int(doc["chunk_count" if chunking else "page_count"] or 0) == 1
    if job_status == "failed":
        assert db_connection.get_latest_document_job(WF)["status"] == "waiting_review"


@pytest.mark.unit
def test_reconcile_unknown_workflow_returns_none(db_connection):
    assert db_connection.reconcile_materialized_state("wf-missing") is None
