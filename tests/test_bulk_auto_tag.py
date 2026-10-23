"""Bulk + per-doc auto-tag routes (issue #46)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import pipeline.db as db_mod
from pipeline.auth.permissions import Permission
from pipeline.models import BulkWorkflowActionRequest
from pipeline.routers import documents_actions
from pipeline.services import access, taxonomy
from tests.helpers import member, seed_document

REVIEWER = member("tenant-a", "content_curator", sub="rev")


@pytest.fixture
def tagged_docs(db_connection):
    """Two tagged-ready docs in tenant-a, one in tenant-b, and an empty-chunk doc in tenant-a."""
    db_mod.create_tenant_row("tenant-a", display_name="Tenant A")
    db_mod.create_tenant_row("tenant-b", display_name="Tenant B")
    for wid, inst in [("wf-tag-1", "tenant-a"), ("wf-tag-2", "tenant-a"), ("wf-tag-b", "tenant-b")]:
        seed_document(
            db_mod, wid, filename=f"{wid}.pdf", stage="completed", chunk_count=2, instance=inst,
            chunks=[{"chunk_number": n, "original_text": text, "page_start": 1, "page_end": 1}
                    for n, text in ((1, "one"), (2, "two"))],
        )
    seed_document(
        db_mod, "wf-empty", document_id="doc-empty", filename="empty.pdf", stage="completed", chunk_count=0,
        instance="tenant-a",
    )


@pytest.fixture
def tagging_enabled(monkeypatch):
    import pipeline.domain_tags.service as svc

    monkeypatch.setattr(svc, "load_domain_tagging_config", lambda: MagicMock(enabled=True))


@pytest.fixture
def tagger(monkeypatch):
    """Stub the per-document tagger; returns the workflow ids it was run for."""
    calls: list[str] = []

    async def _fake_impl(workflow_id, doc):
        calls.append(workflow_id)
        return {"workflow_id": workflow_id, "tagged_chunks": 1, "total_tags": 2}

    monkeypatch.setattr(taxonomy, "auto_tag_document_chunks_impl", _fake_impl)
    return calls


def _bulk(workflow_ids, **kwargs):
    request = BulkWorkflowActionRequest(workflow_ids=workflow_ids, **kwargs)
    return asyncio.run(documents_actions.bulk_auto_tag_documents(request, REVIEWER))


def test_bulk_auto_tag_dry_run(tagged_docs, tagging_enabled):
    res = _bulk(["wf-tag-1", "wf-empty"], dry_run=True)
    assert res.requested == 2
    assert res.succeeded == 1
    assert res.failed == 1
    by_id = {r.workflow_id: r for r in res.results}
    assert by_id["wf-tag-1"].ok is True
    assert "would_execute" in by_id["wf-tag-1"].message
    assert by_id["wf-empty"].message == "no_chunks"


def test_bulk_auto_tag_runs_selected_and_hides_cross_tenant(tagged_docs, tagging_enabled, tagger):
    res = _bulk(["wf-tag-1", "wf-tag-2", "wf-tag-b"])
    assert res.succeeded == 2
    assert res.failed == 1
    by_id = {r.workflow_id: r for r in res.results}
    assert by_id["wf-tag-1"].ok is True
    assert by_id["wf-tag-2"].ok is True
    assert by_id["wf-tag-b"].ok is False
    assert by_id["wf-tag-b"].message == "document_not_found"


def test_bulk_auto_tag_rejects_oversized_batch(tagged_docs, tagging_enabled):
    with pytest.raises(HTTPException) as exc:
        _bulk([f"wf-x-{i}" for i in range(taxonomy.BULK_AUTO_TAG_MAX_DOCS + 1)])
    assert exc.value.status_code == 400


def test_single_auto_tag_blocks_disabled_doc(tagged_docs, tagging_enabled):
    db_mod.set_document_disabled("wf-tag-1", True)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(documents_actions.auto_tag_document_chunks("wf-tag-1", REVIEWER))
    assert exc.value.status_code == 400
    assert "deleted" in str(exc.value.detail).lower()


def test_viewer_cannot_bulk_auto_tag_via_helper_access(tagged_docs):
    # Route uses RequireReview dependency; mirror the permission check used inside.
    viewer = member("tenant-a", "viewer", sub="view")
    assert access.document_for_user_or_none("wf-tag-1", viewer, permission=Permission.REVIEW) is None


def test_bulk_auto_tag_dedups_duplicate_ids(tagged_docs, tagging_enabled, tagger):
    """Duplicate ids collapse to one pass per document (no redundant work)."""
    res = _bulk(["wf-tag-1", "wf-tag-1", "wf-tag-2"])
    assert res.requested == 2
    assert len(res.results) == 2
    assert sorted(tagger) == ["wf-tag-1", "wf-tag-2"]


def test_bulk_auto_tag_audit_anchors_on_real_doc(tagged_docs, tagging_enabled, tagger):
    """Audit rows are anchored on each tagged doc's real workflow_id + hash,
    never on an arbitrary/foreign batch id, and only for docs actually tagged."""
    _bulk(["wf-tag-1", "wf-tag-b"])  # cross-tenant wf-tag-b must NOT produce an audit row
    rows = db_mod.get_audit_logs("wf-tag-1", action_type="bulk_auto_tag")
    assert len(rows) == 1
    assert rows[0]["document_id"] == "doc-wf-tag-1"
    assert db_mod.get_audit_logs("wf-tag-b", action_type="bulk_auto_tag") == []
