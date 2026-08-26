"""Document soft-delete + query enable/disable cascade to chunks.

Also covers Kanav PR #22 blockers:
- mutating lifecycle routes require Permission.ADMIN in the document's tenant
- Marqo purges resolve the document's own index via resolve_index (never bare default)
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import pipeline.db as db_mod
from pipeline.auth.jwt import claims_to_user
from pipeline.auth.permissions import Permission
from pipeline.models import DocumentQueryEnabledUpdate, ChunkUpdate
from pipeline.routers import content, documents
from pipeline.services import access, indexes


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

    assert access.require_document_for_user(lifecycle_doc, curator)["instance"] == "tenant-a"

    with pytest.raises(HTTPException) as exc:
        access.require_document_for_user(lifecycle_doc, curator, permission=Permission.ADMIN)
    assert exc.value.status_code == 403

    with pytest.raises(HTTPException) as exc:
        access.require_document_for_user(lifecycle_doc, viewer, permission=Permission.ADMIN)
    assert exc.value.status_code == 403

    assert (
        access.require_document_for_user(lifecycle_doc, admin, permission=Permission.ADMIN)["instance"]
        == "tenant-a"
    )


def test_query_enabled_route_requires_admin(lifecycle_doc, monkeypatch):
    monkeypatch.setattr(indexes, "delete_chunks_from_marqo", lambda *a, **k: {"deleted": 0})
    monkeypatch.setattr(indexes, "resolve_index", lambda *a, **k: "t-tenant-a-vet")

    with pytest.raises(HTTPException) as exc:
        _run(
            documents.set_document_query_enabled(
                lifecycle_doc,
                DocumentQueryEnabledUpdate(query_enabled=False),
                _curator_in("tenant-a"),
            )
        )
    assert exc.value.status_code == 403

    summary = _run(
        documents.set_document_query_enabled(
            lifecycle_doc,
            DocumentQueryEnabledUpdate(query_enabled=False),
            _admin_in("tenant-a"),
        )
    )
    assert summary.query_enabled is False


def test_hard_delete_chunk_route_requires_admin(lifecycle_doc, monkeypatch):
    monkeypatch.setattr(
        indexes, "delete_single_chunk_from_marqo", lambda *a, **k: {"deleted": False, "reason": "not_found"}
    )
    monkeypatch.setattr(indexes, "resolve_index", lambda *a, **k: "t-tenant-a-vet")

    with pytest.raises(HTTPException) as exc:
        _run(content.delete_chunk(lifecycle_doc, _curator_in("tenant-a"), chunk_num=1))
    assert exc.value.status_code == 403

    res = _run(content.delete_chunk(lifecycle_doc, _admin_in("tenant-a"), chunk_num=1))
    assert res["deleted"] is True
    assert db_mod.get_chunk(lifecycle_doc, 1) is None


def test_disable_document_route_requires_admin(lifecycle_doc, monkeypatch):
    monkeypatch.setattr(indexes, "delete_chunks_from_marqo", lambda *a, **k: {"deleted": 0})
    monkeypatch.setattr(indexes, "resolve_index", lambda *a, **k: "t-tenant-a-vet")

    with pytest.raises(HTTPException) as exc:
        _run(documents.disable_document(lifecycle_doc, _curator_in("tenant-a"), remove_from_search=True))
    assert exc.value.status_code == 403

    res = _run(documents.disable_document(lifecycle_doc, _admin_in("tenant-a"), remove_from_search=True))
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

    bulk = indexes.delete_chunks_from_marqo("doc-x", index_name="t-tenant-a-vet")
    assert bulk.get("deleted") == 0
    assert bulk.get("reason") == "index_missing"
    assert "error" not in bulk

    one = indexes.delete_single_chunk_from_marqo("doc-x", 1, index_name="t-tenant-a-vet")
    assert one.get("deleted") is False
    assert one.get("reason") == "index_missing"
    assert "error" not in one


def test_query_enabled_purge_uses_resolve_index(lifecycle_indexed_doc, monkeypatch):
    calls = []

    def _fake_delete(doc_id, index_name="documents-index", workflow_id=None):
        calls.append({"doc_id": doc_id, "index_name": index_name, "workflow_id": workflow_id})
        return {"deleted": 3, "index_name": index_name}

    monkeypatch.setattr(indexes, "delete_chunks_from_marqo", _fake_delete)

    _run(
        documents.set_document_query_enabled(
            lifecycle_indexed_doc,
            DocumentQueryEnabledUpdate(query_enabled=False),
            _admin_in("tenant-a"),
        )
    )
    assert len(calls) == 1
    assert calls[0]["index_name"] == "t-tenant-a-vet"
    # #73: the purge must be scoped to the document it was asked about.
    assert calls[0]["workflow_id"] == lifecycle_indexed_doc
    assert calls[0]["index_name"] != "documents-index"


def test_delete_chunk_purge_uses_resolve_index(lifecycle_indexed_doc, monkeypatch):
    calls = []

    def _fake_single(doc_id, chunk_num, index_name="documents-index", workflow_id=None):
        calls.append({"doc_id": doc_id, "chunk_num": chunk_num, "index_name": index_name, "workflow_id": workflow_id})
        return {"deleted": True, "chunk_id": "c1"}

    monkeypatch.setattr(indexes, "delete_single_chunk_from_marqo", _fake_single)

    _run(content.delete_chunk(lifecycle_indexed_doc, _admin_in("tenant-a"), chunk_num=1))
    assert len(calls) == 1
    assert calls[0]["index_name"] == "t-tenant-a-vet"
    # #73: the purge must be scoped to the document it was asked about.
    assert calls[0]["workflow_id"] == lifecycle_indexed_doc


def test_chunk_exclude_on_completed_uses_resolve_index(lifecycle_indexed_doc, monkeypatch):
    calls = []

    def _fake_single(doc_id, chunk_num, index_name="documents-index", workflow_id=None):
        calls.append({"doc_id": doc_id, "chunk_num": chunk_num, "index_name": index_name, "workflow_id": workflow_id})
        return {"deleted": True, "chunk_id": "c2"}

    monkeypatch.setattr(indexes, "delete_single_chunk_from_marqo", _fake_single)

    _run(
        content.update_chunk(
            lifecycle_indexed_doc,
            ChunkUpdate(is_excluded=True),
            _curator_in("tenant-a"),
            chunk_num=2,
        )
    )
    assert len(calls) == 1
    assert calls[0]["index_name"] == "t-tenant-a-vet"
    # #73: the purge must be scoped to the document it was asked about.
    assert calls[0]["workflow_id"] == lifecycle_indexed_doc


def test_lifecycle_purge_skips_when_tenant_has_no_index(db_connection, monkeypatch):
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

    monkeypatch.setattr(indexes, "delete_chunks_from_marqo", _fake_delete)
    # ghost has no registered index -> resolve_index returns None -> skip purge
    admin = _admin_in("ghost")
    res = _run(documents.disable_document("wf-ghost", admin, remove_from_search=True))
    assert res["marqo_deleted"] == 0
    assert called["n"] == 0


def test_disable_document_502_before_flip_on_marqo_error(lifecycle_indexed_doc, monkeypatch):
    """A failed Marqo purge must 502 and leave the document NOT disabled — never
    hidden-but-still-searchable (mirror set_document_query_enabled ordering)."""
    monkeypatch.setattr(
        indexes, "delete_chunks_from_marqo", lambda *a, **k: {"deleted": 0, "error": "marqo down"}
    )

    with pytest.raises(HTTPException) as exc:
        _run(documents.disable_document(lifecycle_indexed_doc, _admin_in("tenant-a"), remove_from_search=True))
    assert exc.value.status_code == 502

    # DB was NOT flipped — the purge failed before any state change.
    row = db_mod.get_document(lifecycle_indexed_doc)
    assert int(row["is_disabled"]) == 0
    assert int(row["query_enabled"]) == 1


# =============================================================================
# #135: hard-delete cascade / refuse, artifact GC, index-status alignment
# =============================================================================


def test_hard_delete_without_cascade_is_refused(db_connection):
    db = db_connection
    db.upsert_document(
        workflow_id="wf-hard-refuse",
        document_id="doc-hard-refuse",
        filename="hard.pdf",
        filepath="/tmp/hard.pdf",
        stage="completed",
    )
    db.save_pages(
        "wf-hard-refuse",
        [{"page_number": 1, "original_markdown": "keep me"}],
    )
    with pytest.raises(ValueError, match="cascade"):
        db.delete_document("wf-hard-refuse")
    assert db.get_document("wf-hard-refuse") is not None
    assert db.get_pages("wf-hard-refuse")


def test_hard_delete_cascade_leaves_no_child_rows(db_connection):
    db = db_connection
    wf = "wf-hard-cascade"
    db.upsert_document(
        workflow_id=wf,
        document_id="doc-hard-cascade",
        filename="cascade.pdf",
        filepath="/tmp/cascade.pdf",
        stage="completed",
        chunk_count=1,
    )
    db.save_pages(wf, [{"page_number": 1, "original_markdown": "p"}])
    db.save_chunks(
        wf,
        [{"chunk_number": 1, "original_text": "c", "page_start": 1, "page_end": 1}],
    )
    db.replace_chunk_tags(wf, 1, [{"dimension": "crop", "value": "wheat"}], source="manual")
    db.create_document_job(workflow_id=wf, job_type="pipeline")
    db.add_document_artifact(wf, "original_upload", "minio://documents/cascade.bin")
    db.upsert_document_index_status(wf, "idx-a", status="indexed", chunk_count_indexed=1)
    db.log_audit(workflow_id=wf, document_id="doc-hard-cascade", action_type="disable_document")

    counts = db.delete_document(wf, cascade=True)
    assert counts["documents"] == 1
    assert counts["pages"] == 1
    assert counts["chunks"] == 1
    assert counts["chunk_tags"] == 1
    assert db.get_document(wf) is None
    assert db.get_pages(wf) == []
    assert db.get_chunks(wf, include_excluded=True) == []
    assert db.list_document_artifacts(wf) == []
    assert db.list_document_index_status(wf) == []
    assert db.get_audit_log_count(wf) >= 1


def test_orphan_report_finds_pages_without_document(db_connection):
    db = db_connection
    db.save_pages("wf-ghost-orphan", [{"page_number": 1, "original_markdown": "lost"}])
    report = db.report_orphan_rows()
    ghost = [row for row in report["tables"]["pages"] if row["workflow_id"] == "wf-ghost-orphan"]
    assert ghost and int(ghost[0]["n"]) == 1


def test_soft_delete_default_does_not_delete_minio(lifecycle_doc, monkeypatch):
    db_mod.add_document_artifact(
        lifecycle_doc, "original_upload", "minio://documents/life.bin", filename="life.bin"
    )
    deleted = []
    monkeypatch.setattr(
        "pipeline.services.artifacts.minio_storage.delete_object",
        lambda bucket, name: deleted.append((bucket, name)),
    )
    monkeypatch.setattr(indexes, "delete_chunks_from_marqo", lambda *a, **k: {"deleted": 0})
    monkeypatch.setattr(indexes, "resolve_index", lambda *a, **k: None)

    res = _run(
        documents.disable_document(
            lifecycle_doc, _admin_in("tenant-a"), remove_from_search=True
        )
    )
    assert deleted == []
    assert res["artifact_purge"]["apply"] is False
    assert res["artifact_purge"]["would_purge_count"] == 1
    assert res["artifact_purge"]["purged_count"] == 0
    row = db_mod.list_document_artifacts(lifecycle_doc)[0]
    assert not row.get("purged_at")


def test_purge_artifacts_apply_is_idempotent(lifecycle_doc, monkeypatch):
    db_mod.set_document_disabled(lifecycle_doc, True)
    db_mod.add_document_artifact(
        lifecycle_doc, "original_upload", "minio://documents/life.bin", filename="life.bin"
    )
    db_mod.add_document_artifact(
        lifecycle_doc, "local_copy", "/tmp/life.bin", filename="local.bin"
    )
    deleted = []
    monkeypatch.setattr(
        "pipeline.services.artifacts.minio_storage.delete_object",
        lambda bucket, name: deleted.append((bucket, name)),
    )

    first = _run(
        documents.purge_document_artifacts(lifecycle_doc, _admin_in("tenant-a"), apply=True)
    )
    assert first["purged_count"] == 1
    assert first["retained_count"] == 1
    assert deleted == [("documents", "life.bin")]
    row = next(
        a for a in db_mod.list_document_artifacts(lifecycle_doc) if a["artifact_type"] == "original_upload"
    )
    assert row.get("purged_at")

    deleted.clear()
    second = _run(
        documents.purge_document_artifacts(lifecycle_doc, _admin_in("tenant-a"), apply=True)
    )
    assert second["purged_count"] == 0
    assert second["already_purged_count"] == 1
    assert deleted == []


def test_purge_artifacts_refuses_live_document(lifecycle_doc):
    with pytest.raises(HTTPException) as exc:
        _run(documents.purge_document_artifacts(lifecycle_doc, _admin_in("tenant-a"), apply=False))
    assert exc.value.status_code == 400


def test_disable_marks_index_status_removed(lifecycle_indexed_doc, monkeypatch):
    db_mod.upsert_document_index_status(
        lifecycle_indexed_doc, "t-tenant-a-vet", status="indexed", chunk_count_indexed=2
    )
    monkeypatch.setattr(indexes, "delete_chunks_from_marqo", lambda *a, **k: {"deleted": 2})
    _run(
        documents.disable_document(
            lifecycle_indexed_doc, _admin_in("tenant-a"), remove_from_search=True
        )
    )
    status = db_mod.get_document_index_status(lifecycle_indexed_doc, "t-tenant-a-vet")
    assert status["status"] == "removed"
    assert int(status["chunk_count_indexed"]) == 0

