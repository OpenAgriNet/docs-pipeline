"""Bulk + per-doc auto-tag routes (issue #46)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import pipeline.db as db_mod
from pipeline.auth.jwt import claims_to_user
from pipeline.auth.permissions import Permission
from pipeline.models import BulkWorkflowActionRequest
from pipeline.routers import documents_actions
from pipeline.services import access, taxonomy


def _run(coro):
    return asyncio.run(coro)


def _reviewer(instance: str = "tenant-a"):
    return claims_to_user({"sub": "rev", "tenant_roles": {instance: ["content_curator"]}})


def _viewer(instance: str = "tenant-a"):
    return claims_to_user({"sub": "view", "tenant_roles": {instance: ["viewer"]}})


@pytest.fixture
def tagged_docs(db_connection, monkeypatch):
    db_mod.create_tenant_row("tenant-a", display_name="Tenant A")
    db_mod.create_tenant_row("tenant-b", display_name="Tenant B")

    for wid, inst in [("wf-tag-1", "tenant-a"), ("wf-tag-2", "tenant-a"), ("wf-tag-b", "tenant-b")]:
        db_mod.upsert_document(
            document_id=f"doc-{wid}",
            workflow_id=wid,
            filename=f"{wid}.pdf",
            filepath=f"/tmp/{wid}.pdf",
            stage="completed",
            chunk_count=2,
            instance=inst,
        )
        db_mod.save_chunks(
            wid,
            [
                {"chunk_number": 1, "original_text": "one", "page_start": 1, "page_end": 1},
                {"chunk_number": 2, "original_text": "two", "page_start": 1, "page_end": 1},
            ],
        )

    # Empty-chunk doc in tenant-a
    db_mod.upsert_document(
        document_id="doc-empty",
        workflow_id="wf-empty",
        filename="empty.pdf",
        filepath="/tmp/empty.pdf",
        stage="completed",
        chunk_count=0,
        instance="tenant-a",
    )
    return ["wf-tag-1", "wf-tag-2", "wf-tag-b", "wf-empty"]


def _mock_tagger(monkeypatch, *, tagged_chunks=1, total_tags=2):
    async def _fake_impl(workflow_id, doc):
        return {
            "workflow_id": workflow_id,
            "tagged_chunks": tagged_chunks,
            "total_tags": total_tags,
        }

    monkeypatch.setattr(taxonomy, "auto_tag_document_chunks_impl", _fake_impl)


def test_bulk_auto_tag_dry_run(tagged_docs, monkeypatch):
    import pipeline.domain_tags.service as svc

    monkeypatch.setattr(svc, "load_domain_tagging_config", lambda: MagicMock(enabled=True))

    res = _run(
        documents_actions.bulk_auto_tag_documents(
            BulkWorkflowActionRequest(workflow_ids=["wf-tag-1", "wf-empty"], dry_run=True),
            _reviewer(),
        )
    )
    assert res.requested == 2
    assert res.succeeded == 1
    assert res.failed == 1
    by_id = {r.workflow_id: r for r in res.results}
    assert by_id["wf-tag-1"].ok is True
    assert "would_execute" in by_id["wf-tag-1"].message
    assert by_id["wf-empty"].message == "no_chunks"


def test_bulk_auto_tag_runs_selected_and_hides_cross_tenant(tagged_docs, monkeypatch):
    import pipeline.domain_tags.service as svc

    monkeypatch.setattr(svc, "load_domain_tagging_config", lambda: MagicMock(enabled=True))
    _mock_tagger(monkeypatch)

    res = _run(
        documents_actions.bulk_auto_tag_documents(
            BulkWorkflowActionRequest(workflow_ids=["wf-tag-1", "wf-tag-2", "wf-tag-b"]),
            _reviewer("tenant-a"),
        )
    )
    assert res.succeeded == 2
    assert res.failed == 1
    by_id = {r.workflow_id: r for r in res.results}
    assert by_id["wf-tag-1"].ok is True
    assert by_id["wf-tag-2"].ok is True
    assert by_id["wf-tag-b"].ok is False
    assert by_id["wf-tag-b"].message == "document_not_found"


def test_bulk_auto_tag_rejects_oversized_batch(tagged_docs, monkeypatch):
    import pipeline.domain_tags.service as svc

    monkeypatch.setattr(svc, "load_domain_tagging_config", lambda: MagicMock(enabled=True))
    ids = [f"wf-x-{i}" for i in range(taxonomy.BULK_AUTO_TAG_MAX_DOCS + 1)]
    with pytest.raises(HTTPException) as exc:
        _run(documents_actions.bulk_auto_tag_documents(BulkWorkflowActionRequest(workflow_ids=ids), _reviewer()))
    assert exc.value.status_code == 400


def test_single_auto_tag_blocks_disabled_doc(tagged_docs, monkeypatch):
    import pipeline.domain_tags.service as svc

    monkeypatch.setattr(svc, "load_domain_tagging_config", lambda: MagicMock(enabled=True))
    db_mod.set_document_disabled("wf-tag-1", True)
    with pytest.raises(HTTPException) as exc:
        _run(documents_actions.auto_tag_document_chunks("wf-tag-1", _reviewer()))
    assert exc.value.status_code == 400
    assert "deleted" in str(exc.value.detail).lower()


def test_viewer_cannot_bulk_auto_tag_via_helper_access(tagged_docs):
    # Route uses RequireReview dependency; mirror the permission check used inside.
    doc = access.document_for_user_or_none(
        "wf-tag-1", _viewer(), permission=Permission.REVIEW
    )
    assert doc is None


def test_bulk_auto_tag_dedups_duplicate_ids(tagged_docs, monkeypatch):
    """Duplicate ids collapse to one pass per document (no redundant work)."""
    import pipeline.domain_tags.service as svc

    monkeypatch.setattr(svc, "load_domain_tagging_config", lambda: MagicMock(enabled=True))
    calls: list[str] = []

    async def _fake_impl(workflow_id, doc):
        calls.append(workflow_id)
        return {"workflow_id": workflow_id, "tagged_chunks": 1, "total_tags": 2}

    monkeypatch.setattr(taxonomy, "auto_tag_document_chunks_impl", _fake_impl)

    res = _run(
        documents_actions.bulk_auto_tag_documents(
            BulkWorkflowActionRequest(workflow_ids=["wf-tag-1", "wf-tag-1", "wf-tag-2"]),
            _reviewer("tenant-a"),
        )
    )
    assert res.requested == 2
    assert len(res.results) == 2
    assert sorted(calls) == ["wf-tag-1", "wf-tag-2"]


def test_bulk_auto_tag_audit_anchors_on_real_doc(tagged_docs, monkeypatch):
    """Audit rows are anchored on each tagged doc's real workflow_id + hash,
    never on an arbitrary/foreign batch id, and only for docs actually tagged."""
    import pipeline.domain_tags.service as svc

    monkeypatch.setattr(svc, "load_domain_tagging_config", lambda: MagicMock(enabled=True))
    _mock_tagger(monkeypatch)

    _run(
        documents_actions.bulk_auto_tag_documents(
            # cross-tenant wf-tag-b must NOT produce an audit row
            BulkWorkflowActionRequest(workflow_ids=["wf-tag-1", "wf-tag-b"]),
            _reviewer("tenant-a"),
        )
    )
    rows = db_mod.get_audit_logs("wf-tag-1", action_type="bulk_auto_tag")
    assert len(rows) == 1
    assert rows[0]["document_id"] == "doc-wf-tag-1"
    # No audit row minted for the hidden cross-tenant doc.
    assert db_mod.get_audit_logs("wf-tag-b", action_type="bulk_auto_tag") == []
