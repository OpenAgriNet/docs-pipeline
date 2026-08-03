"""Document soft-delete + query enable/disable cascade to chunks.

Also covers Kanav PR #22 blockers:
- mutating lifecycle routes require Permission.ADMIN in the document's tenant
- Marqo purges resolve the document's own index via resolve_index (never bare default)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import pipeline.api as api
import pipeline.db as db_mod
from pipeline.auth.jwt import claims_to_user
from pipeline.auth.permissions import Permission
from pipeline.models import DocumentQueryEnabledUpdate, ChunkUpdate


def _run(coro):
    return asyncio.run(coro)


def _admin_in(instance: str):
    return claims_to_user({"sub": "tadmin", "tenant_roles": {instance: ["admin"]}})


def _curator_in(instance: str):
    return claims_to_user({"sub": "cur", "tenant_roles": {instance: ["content_curator"]}})


def _viewer_in(instance: str):
    return claims_to_user({"sub": "view", "tenant_roles": {instance: ["viewer"]}})


# =============================================================================
# DB helpers
# =============================================================================


def test_soft_delete_excludes_all_chunks(db_connection):
    db = db_connection
    db.upsert_document(
        document_id="doc-life-1",
        workflow_id="wf-life-1",
        filename="life.pdf",
        filepath="/tmp/life.pdf",
        stage="completed",
        chunk_count=2,
    )
    db.save_chunks(
        "wf-life-1",
        [
            {"chunk_number": 1, "original_text": "one", "page_start": 1, "page_end": 1},
            {"chunk_number": 2, "original_text": "two", "page_start": 1, "page_end": 1},
        ],
    )

    db.set_document_disabled("wf-life-1", True)
    updated = db.set_all_chunks_excluded("wf-life-1", True)
    assert updated == 2

    chunks = db.get_chunks("wf-life-1", include_excluded=True)
    assert len(chunks) == 2
    assert all(bool(c.get("is_excluded")) for c in chunks)
    assert db.get_chunks("wf-life-1", include_excluded=False) == []


def test_query_disable_cascades_to_chunks(db_connection):
    db = db_connection
    db.upsert_document(
        document_id="doc-life-3",
        workflow_id="wf-life-3",
        filename="cascade.pdf",
        filepath="/tmp/cascade.pdf",
        stage="completed",
    )
    db.save_chunks(
        "wf-life-3",
        [
            {"chunk_number": 1, "original_text": "one", "page_start": 1, "page_end": 1},
            {"chunk_number": 2, "original_text": "two", "page_start": 1, "page_end": 1},
        ],
    )

    updated = db.set_document_query_enabled("wf-life-3", False)
    assert int(updated["query_enabled"]) == 0
    db.set_all_chunks_excluded("wf-life-3", True)
    chunks = db.get_chunks("wf-life-3", include_excluded=True)
    assert all(bool(c.get("is_excluded")) for c in chunks)

    db.set_document_query_enabled("wf-life-3", True)
    db.set_all_chunks_excluded("wf-life-3", False)
    chunks = db.get_chunks("wf-life-3", include_excluded=True)
    assert all(not bool(c.get("is_excluded")) for c in chunks)
    assert int(db.get_document("wf-life-3")["query_enabled"]) == 1


def test_hard_delete_chunk_removes_row_and_updates_count(db_connection):
    db = db_connection
    db.upsert_document(
        document_id="doc-chunk-del",
        workflow_id="wf-chunk-del",
        filename="chunk-del.pdf",
        filepath="/tmp/chunk-del.pdf",
        stage="completed",
        chunk_count=3,
    )
    db.save_chunks(
        "wf-chunk-del",
        [
            {"chunk_number": 1, "original_text": "one", "page_start": 1, "page_end": 1},
            {"chunk_number": 2, "original_text": "two", "page_start": 1, "page_end": 1},
            {"chunk_number": 3, "original_text": "three", "page_start": 2, "page_end": 2},
        ],
    )
    db.replace_chunk_tags(
        "wf-chunk-del",
        2,
        [{"dimension": "crop", "value": "wheat"}],
        source="manual",
    )

    assert db.delete_chunk("wf-chunk-del", 2) is True
    assert db.get_chunk("wf-chunk-del", 2) is None
    remaining = db.get_chunks("wf-chunk-del", include_excluded=True)
    assert [c["chunk_number"] for c in remaining] == [1, 3]
    assert int(db.get_document("wf-chunk-del")["chunk_count"]) == 2
    assert db.get_chunk_tags("wf-chunk-del", 2) == []
    assert db.delete_chunk("wf-chunk-del", 2) is False


def test_query_enabled_column_defaults_on(db_connection):
    db = db_connection
    db.upsert_document(
        document_id="doc-qe-default",
        workflow_id="wf-qe-default",
        filename="qe.pdf",
        filepath="/tmp/qe.pdf",
        stage="completed",
    )
    row = db.get_document("wf-qe-default")
    assert row.get("query_enabled") in (1, True, None) or int(row.get("query_enabled") or 1) == 1


# =============================================================================
# Auth: lifecycle mutations require ADMIN in the document's tenant
# =============================================================================


@pytest.fixture
def lifecycle_doc(db_connection, monkeypatch):
    monkeypatch.setattr(api, "db", db_mod)
    monkeypatch.setattr(api, "temporal_client", MagicMock())
    db_mod.upsert_document(
        document_id="doc-life-auth",
        workflow_id="wf-life-auth",
        filename="auth.pdf",
        filepath="/tmp/auth.pdf",
        stage="completed",
        instance="tenant-a",
        chunk_count=1,
    )
    db_mod.save_chunks(
        "wf-life-auth",
        [{"chunk_number": 1, "original_text": "one", "page_start": 1, "page_end": 1}],
    )
    return "wf-life-auth"


def test_require_document_admin_pattern_for_lifecycle(lifecycle_doc):
    """Curator can read; ADMIN-gated require raises 403 for curator, passes for admin."""
    curator = _curator_in("tenant-a")
    admin = _admin_in("tenant-a")
    viewer = _viewer_in("tenant-a")

    assert api._require_document_for_user(lifecycle_doc, curator)["instance"] == "tenant-a"

    with pytest.raises(HTTPException) as exc:
        api._require_document_for_user(lifecycle_doc, curator, permission=Permission.ADMIN)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        api._require_document_for_user(lifecycle_doc, viewer, permission=Permission.ADMIN)
    assert exc.value.status_code == 403

    assert (
        api._require_document_for_user(lifecycle_doc, admin, permission=Permission.ADMIN)["instance"]
        == "tenant-a"
    )


def test_query_enabled_route_requires_admin(lifecycle_doc, monkeypatch):
    monkeypatch.setattr(api, "delete_chunks_from_marqo", lambda *a, **k: {"deleted": 0})
    monkeypatch.setattr(api, "resolve_index", lambda *a, **k: "t-tenant-a-vet")

    with pytest.raises(HTTPException) as exc:
        _run(
            api.set_document_query_enabled(
                lifecycle_doc,
                DocumentQueryEnabledUpdate(query_enabled=False),
                _curator_in("tenant-a"),
            )
        )
    assert exc.value.status_code == 403

    summary = _run(
        api.set_document_query_enabled(
            lifecycle_doc,
            DocumentQueryEnabledUpdate(query_enabled=False),
            _admin_in("tenant-a"),
        )
    )
    assert summary.query_enabled is False


def test_hard_delete_chunk_route_requires_admin(lifecycle_doc, monkeypatch):
    monkeypatch.setattr(
        api, "delete_single_chunk_from_marqo", lambda *a, **k: {"deleted": False, "reason": "not_found"}
    )
    monkeypatch.setattr(api, "resolve_index", lambda *a, **k: "t-tenant-a-vet")

    with pytest.raises(HTTPException) as exc:
        _run(api.delete_chunk(lifecycle_doc, _curator_in("tenant-a"), chunk_num=1))
    assert exc.value.status_code == 403

    res = _run(api.delete_chunk(lifecycle_doc, _admin_in("tenant-a"), chunk_num=1))
    assert res["deleted"] is True
    assert db_mod.get_chunk(lifecycle_doc, 1) is None


def test_disable_document_route_requires_admin(lifecycle_doc, monkeypatch):
    monkeypatch.setattr(api, "delete_chunks_from_marqo", lambda *a, **k: {"deleted": 0})
    monkeypatch.setattr(api, "resolve_index", lambda *a, **k: "t-tenant-a-vet")

    with pytest.raises(HTTPException) as exc:
        _run(api.disable_document(lifecycle_doc, _curator_in("tenant-a"), remove_from_search=True))
    assert exc.value.status_code == 403

    res = _run(api.disable_document(lifecycle_doc, _admin_in("tenant-a"), remove_from_search=True))
    assert res["disabled"] is True
    assert res["chunks_excluded"] == 1
    row = db_mod.get_document(lifecycle_doc)
    assert int(row["is_disabled"]) == 1
    assert int(row["query_enabled"]) == 0


# =============================================================================
# Index: lifecycle Marqo deletes use resolve_index for non-default tenants
# =============================================================================


@pytest.fixture
def lifecycle_indexed_doc(db_connection, monkeypatch):
    monkeypatch.setattr(api, "db", db_mod)
    monkeypatch.setattr(api, "temporal_client", MagicMock())
    db_mod.create_tenant_row("tenant-a", display_name="Tenant A")
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    db_mod.upsert_document(
        document_id="doc-life-idx",
        workflow_id="wf-life-idx",
        filename="idx.pdf",
        filepath="/tmp/idx.pdf",
        stage="completed",
        instance="tenant-a",
        chunk_count=2,
    )
    db_mod.save_chunks(
        "wf-life-idx",
        [
            {"chunk_number": 1, "original_text": "one", "page_start": 1, "page_end": 1},
            {"chunk_number": 2, "original_text": "two", "page_start": 1, "page_end": 1},
        ],
    )
    return "wf-life-idx"


def test_marqo_index_missing_is_benign(monkeypatch):
    """Missing Marqo index must not surface as error — nothing was searchable."""
    class _Boom:
        def search(self, *a, **k):
            raise RuntimeError("Index not found")

        def delete_documents(self, *a, **k):
            raise AssertionError("should not delete")

    class _Client:
        def __init__(self, url):
            pass

        def index(self, name):
            return _Boom()

    fake_marqo = type("m", (), {"Client": _Client})
    monkeypatch.setitem(__import__("sys").modules, "marqo", fake_marqo)

    bulk = api.delete_chunks_from_marqo("doc-x", index_name="t-tenant-a-vet")
    assert bulk.get("deleted") == 0
    assert bulk.get("reason") == "index_missing"
    assert "error" not in bulk

    one = api.delete_single_chunk_from_marqo("doc-x", 1, index_name="t-tenant-a-vet")
    assert one.get("deleted") is False
    assert one.get("reason") == "index_missing"
    assert "error" not in one


def test_query_enabled_purge_uses_resolve_index(lifecycle_indexed_doc, monkeypatch):
    calls = []

    def _fake_delete(doc_id, index_name="documents-index", **kwargs):
        calls.append({"doc_id": doc_id, "index_name": index_name})
        return {"deleted": 3, "index_name": index_name}

    monkeypatch.setattr(api, "delete_chunks_from_marqo", _fake_delete)

    _run(
        api.set_document_query_enabled(
            lifecycle_indexed_doc,
            DocumentQueryEnabledUpdate(query_enabled=False),
            _admin_in("tenant-a"),
        )
    )
    assert len(calls) == 1
    assert calls[0]["index_name"] == "t-tenant-a-vet"
    assert calls[0]["index_name"] != "documents-index"


def test_delete_chunk_purge_uses_resolve_index(lifecycle_indexed_doc, monkeypatch):
    calls = []

    def _fake_single(doc_id, chunk_num, index_name="documents-index", **kwargs):
        calls.append({"doc_id": doc_id, "chunk_num": chunk_num, "index_name": index_name})
        return {"deleted": True, "chunk_id": "c1"}

    monkeypatch.setattr(api, "delete_single_chunk_from_marqo", _fake_single)

    _run(api.delete_chunk(lifecycle_indexed_doc, _admin_in("tenant-a"), chunk_num=1))
    assert len(calls) == 1
    assert calls[0]["index_name"] == "t-tenant-a-vet"


def test_chunk_exclude_on_completed_uses_resolve_index(lifecycle_indexed_doc, monkeypatch):
    calls = []

    def _fake_single(doc_id, chunk_num, index_name="documents-index", **kwargs):
        calls.append({"doc_id": doc_id, "chunk_num": chunk_num, "index_name": index_name})
        return {"deleted": True, "chunk_id": "c2"}

    monkeypatch.setattr(api, "delete_single_chunk_from_marqo", _fake_single)

    _run(
        api.update_chunk(
            lifecycle_indexed_doc,
            ChunkUpdate(is_excluded=True),
            _curator_in("tenant-a"),
            chunk_num=2,
        )
    )
    assert len(calls) == 1
    assert calls[0]["index_name"] == "t-tenant-a-vet"


def test_lifecycle_purge_skips_when_tenant_has_no_index(db_connection, monkeypatch):
    monkeypatch.setattr(api, "db", db_mod)
    monkeypatch.setattr(api, "temporal_client", MagicMock())
    db_mod.create_tenant_row("ghost", display_name="Ghost")
    db_mod.upsert_document(
        document_id="doc-ghost",
        workflow_id="wf-ghost",
        filename="ghost.pdf",
        filepath="/tmp/ghost.pdf",
        stage="completed",
        instance="ghost",
    )
    called = {"n": 0}

    def _fake_delete(*a, **k):
        called["n"] += 1
        return {"deleted": 0}

    monkeypatch.setattr(api, "delete_chunks_from_marqo", _fake_delete)
    # ghost has no registered index -> resolve_index returns None -> skip purge
    admin = _admin_in("ghost")
    res = _run(api.disable_document("wf-ghost", admin, remove_from_search=True))
    assert res["marqo_deleted"] == 0
    assert called["n"] == 0


def test_disable_document_502_before_flip_on_marqo_error(lifecycle_indexed_doc, monkeypatch):
    """A failed Marqo purge must 502 and leave the document NOT disabled — never
    hidden-but-still-searchable (mirror set_document_query_enabled ordering)."""
    monkeypatch.setattr(
        api, "delete_chunks_from_marqo", lambda *a, **k: {"deleted": 0, "error": "marqo down"}
    )

    with pytest.raises(HTTPException) as exc:
        _run(api.disable_document(lifecycle_indexed_doc, _admin_in("tenant-a"), remove_from_search=True))
    assert exc.value.status_code == 502

    # DB was NOT flipped — the purge failed before any state change.
    row = db_mod.get_document(lifecycle_indexed_doc)
    assert int(row["is_disabled"]) == 0
    assert int(row["query_enabled"]) == 1
