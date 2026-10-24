"""Document soft delete, query-off and hard delete: chunk cascade, Marqo purge, MinIO GC.

Lifecycle mutations need ADMIN in the document's tenant (PR #22), and every Marqo
purge targets the document's own resolved index, scoped to its workflow (#73).
"""

from __future__ import annotations

import asyncio

import pytest

import pipeline.db as db_mod
from pipeline.models import ChunkUpdate, DocumentQueryEnabledUpdate
from pipeline.routers import content, documents
from pipeline.services import indexes
from tests.helpers import http_status, member, seed_document

A = "tenant-a"
WF = "wf-life"
INDEX = "t-tenant-a-vet"
OFF = DocumentQueryEnabledUpdate(query_enabled=False)
ON = DocumentQueryEnabledUpdate(query_enabled=True)


@pytest.fixture
def doc(db_connection):
    """A completed tenant-a document with two chunks; the tenant has a default index."""
    db_mod.create_tenant_row(A, display_name="Tenant A")
    db_mod.create_index_row(A, "vet", INDEX, is_default=True)
    seed_document(db_connection, WF, stage="completed", instance=A, chunk_count=2, chunks=["one", "two"])
    return WF


def _purges(monkeypatch, failing=(), empty=False):
    """Stub both Marqo purges; returns ``(index, workflow_id)`` per call.

    ``failing`` indexes report an error; ``empty`` makes every purge a successful no-op
    (nothing was indexed), which must not block the lifecycle change.
    """
    calls = []

    def bulk(doc_id, index_name="documents-index", workflow_id=None):
        calls.append((index_name, workflow_id))
        if index_name in failing:
            return {"deleted": 0, "error": "marqo down"}
        return {"deleted": 0} if empty else {"deleted": 2}

    def single(doc_id, chunk_num, index_name="documents-index", workflow_id=None):
        calls.append((index_name, workflow_id))
        return {"deleted": False, "reason": "not_found"} if empty else {"deleted": True, "chunk_id": f"c{chunk_num}"}

    monkeypatch.setattr(indexes, "delete_chunks_from_marqo", bulk)
    monkeypatch.setattr(indexes, "delete_single_chunk_from_marqo", single)
    return calls


def _indexed_in(workflow_id, *index_names):
    for name in index_names:
        db_mod.upsert_document_index_status(workflow_id, name, status="indexed", chunk_count_indexed=2)


def _minio_deletes(monkeypatch):
    deleted = []
    monkeypatch.setattr(
        "pipeline.services.artifacts.minio_storage.delete_object", lambda bucket, name: deleted.append((bucket, name))
    )
    return deleted


# --- hard delete ------------------------------------------------------------------


def test_hard_delete_chunk_removes_row_and_updates_count(db_connection):
    seed_document(db_connection, WF, stage="completed", chunk_count=3, chunks=["one", "two", "three"])
    db_mod.replace_chunk_tags(WF, 2, [{"dimension": "crop", "value": "wheat"}], source="manual")

    assert db_mod.delete_chunk(WF, 2) is True
    assert db_mod.get_chunk(WF, 2) is None
    assert [c["chunk_number"] for c in db_mod.get_chunks(WF, include_excluded=True)] == [1, 3]
    assert int(db_mod.get_document(WF)["chunk_count"]) == 2
    assert db_mod.get_chunk_tags(WF, 2) == []
    assert db_mod.delete_chunk(WF, 2) is False


# --- routes: ADMIN in the document's tenant, purge the document's own index ---------

purge_outcomes = pytest.mark.parametrize("empty", [False, True], ids=["purged", "nothing-indexed"])


@purge_outcomes
def test_query_off_requires_admin_and_purges_the_documents_own_index(doc, monkeypatch, empty):
    calls = _purges(monkeypatch, empty=empty)
    assert http_status(documents.set_document_query_enabled(doc, OFF, member(A, "content_curator"))) == 403

    summary = asyncio.run(documents.set_document_query_enabled(doc, OFF, member(A, "admin")))
    assert summary.query_enabled is False
    assert calls == [(INDEX, doc)]  # never the bare default, and scoped to this document
    assert db_mod.get_chunks(doc, include_excluded=False) == []

    summary = asyncio.run(documents.set_document_query_enabled(doc, ON, member(A, "admin")))
    assert summary.query_enabled is True
    assert len(db_mod.get_chunks(doc, include_excluded=False)) == 2


@purge_outcomes
def test_delete_chunk_requires_admin_and_purges_the_documents_own_index(doc, monkeypatch, empty):
    calls = _purges(monkeypatch, empty=empty)
    assert http_status(content.delete_chunk(doc, member(A, "content_curator"), chunk_num=1)) == 403

    assert asyncio.run(content.delete_chunk(doc, member(A, "admin"), chunk_num=1))["deleted"] is True
    assert db_mod.get_chunk(doc, 1) is None
    assert calls == [(INDEX, doc)]


@purge_outcomes
def test_disable_requires_admin_then_excludes_chunks_and_marks_index_removed(doc, monkeypatch, empty):
    _indexed_in(doc, INDEX)
    _purges(monkeypatch, empty=empty)
    assert http_status(documents.disable_document(doc, member(A, "content_curator"), remove_from_search=True)) == 403

    res = asyncio.run(documents.disable_document(doc, member(A, "admin"), remove_from_search=True))
    assert res["disabled"] is True
    assert res["chunks_excluded"] == 2
    assert db_mod.get_chunks(doc, include_excluded=False) == []
    row = db_mod.get_document(doc)
    assert int(row["is_disabled"]) == 1
    assert int(row["query_enabled"]) == 0
    status = db_mod.get_document_index_status(doc, INDEX)
    assert status["status"] == "removed"
    assert int(status["chunk_count_indexed"]) == 0


def test_chunk_exclude_on_completed_purges_the_documents_own_index(doc, monkeypatch):
    calls = _purges(monkeypatch)
    asyncio.run(content.update_chunk(doc, ChunkUpdate(is_excluded=True), member(A, "content_curator"), chunk_num=2))
    assert calls == [(INDEX, doc)]


def test_lifecycle_purge_skips_when_tenant_has_no_index(db_connection, monkeypatch):
    db_mod.create_tenant_row("ghost", display_name="Ghost")
    seed_document(db_connection, "wf-ghost", stage="completed", instance="ghost")
    calls = _purges(monkeypatch)
    res = asyncio.run(documents.disable_document("wf-ghost", member("ghost", "admin"), remove_from_search=True))
    assert res["marqo_deleted"] == 0
    assert calls == []


def test_disable_document_502_before_flip_on_marqo_error(doc, monkeypatch):
    """A failed purge leaves the document NOT disabled, never hidden-but-still-searchable."""
    _purges(monkeypatch, failing={INDEX})
    assert http_status(documents.disable_document(doc, member(A, "admin"), remove_from_search=True)) == 502
    row = db_mod.get_document(doc)
    assert int(row["is_disabled"]) == 0
    assert int(row["query_enabled"]) == 1


def test_disable_purges_every_recorded_index_before_marking_removed(doc, monkeypatch):
    """Historical indexes stay searchable unless they are purged, not just marked."""
    _indexed_in(doc, INDEX, "old-index")
    calls = _purges(monkeypatch)
    asyncio.run(documents.disable_document(doc, member(A, "admin"), remove_from_search=True))
    assert {name for name, _ in calls} == {INDEX, "old-index"}
    assert db_mod.get_document_index_status(doc, INDEX)["status"] == "removed"
    assert db_mod.get_document_index_status(doc, "old-index")["status"] == "removed"


def test_query_off_does_not_mark_unpurged_historical_index(doc, monkeypatch):
    _indexed_in(doc, INDEX, "old-index")
    _purges(monkeypatch, failing={"old-index"})
    assert http_status(documents.set_document_query_enabled(doc, OFF, member(A, "admin"))) == 502
    assert int(db_mod.get_document(doc)["query_enabled"]) == 1
    assert db_mod.get_document_index_status(doc, "old-index")["status"] == "indexed"


# --- #135: hard delete cascade / refuse, artifact GC --------------------------------


def test_hard_delete_without_cascade_is_refused(db_connection):
    seed_document(db_connection, WF, stage="completed", pages=["keep me"])
    with pytest.raises(ValueError, match="cascade"):
        db_mod.delete_document(WF)
    assert db_mod.get_document(WF) is not None
    assert db_mod.get_pages(WF)


def test_hard_delete_cascade_leaves_no_child_rows(db_connection):
    seed_document(db_connection, WF, stage="completed", chunk_count=1, pages=["p"], chunks=["c"])
    db_mod.replace_chunk_tags(WF, 1, [{"dimension": "crop", "value": "wheat"}], source="manual")
    db_mod.create_document_job(workflow_id=WF, job_type="pipeline")
    db_mod.mark_artifact_purged(db_mod.add_document_artifact(WF, "original_upload", "minio://documents/cascade.bin"))
    db_mod.add_document_artifact(WF, "local_copy", "/tmp/cascade.bin")
    db_mod.upsert_document_index_status(WF, "idx-a", status="indexed", chunk_count_indexed=1)
    db_mod.log_audit(workflow_id=WF, document_id=f"doc-{WF}", action_type="disable_document")

    counts = db_mod.delete_document(WF, cascade=True)
    assert counts["documents"] == 1
    assert counts["pages"] == 1
    assert counts["chunks"] == 1
    assert counts["chunk_tags"] == 1
    assert db_mod.get_document(WF) is None
    assert db_mod.get_pages(WF) == []
    assert db_mod.get_chunks(WF, include_excluded=True) == []
    assert db_mod.list_document_artifacts(WF) == []
    assert db_mod.list_document_index_status(WF) == []
    assert db_mod.get_audit_log_count(WF) >= 1


def test_hard_delete_cascade_refuses_unpurged_minio_artifacts(db_connection):
    seed_document(db_connection, WF, stage="completed")
    db_mod.add_document_artifact(WF, "original_upload", "minio://documents/unpurged.bin")
    with pytest.raises(ValueError, match="unpurged minio"):
        db_mod.delete_document(WF, cascade=True)
    assert db_mod.get_document(WF) is not None
    artifacts = db_mod.list_document_artifacts(WF)
    assert len(artifacts) == 1
    assert not artifacts[0].get("purged_at")


def test_orphan_report_finds_pages_without_document(db_connection):
    db_mod.save_pages("wf-ghost-orphan", [{"page_number": 1, "original_markdown": "lost"}])
    ghost = [r for r in db_mod.report_orphan_rows()["tables"]["pages"] if r["workflow_id"] == "wf-ghost-orphan"]
    assert ghost and int(ghost[0]["n"]) == 1


def test_soft_delete_default_does_not_delete_minio(doc, monkeypatch):
    db_mod.add_document_artifact(doc, "original_upload", "minio://documents/life.bin", filename="life.bin")
    deleted = _minio_deletes(monkeypatch)
    _purges(monkeypatch)

    res = asyncio.run(documents.disable_document(doc, member(A, "admin"), remove_from_search=True))
    assert deleted == []
    assert res["artifact_purge"]["apply"] is False
    assert res["artifact_purge"]["would_purge_count"] == 1
    assert res["artifact_purge"]["purged_count"] == 0
    assert not db_mod.list_document_artifacts(doc)[0].get("purged_at")


def test_purge_artifacts_apply_is_idempotent(doc, monkeypatch):
    db_mod.set_document_disabled(doc, True)
    db_mod.add_document_artifact(doc, "original_upload", "minio://documents/life.bin", filename="life.bin")
    db_mod.add_document_artifact(doc, "local_copy", "/tmp/life.bin", filename="local.bin")
    deleted = _minio_deletes(monkeypatch)

    first = asyncio.run(documents.purge_document_artifacts(doc, member(A, "admin"), apply=True))
    assert first["purged_count"] == 1
    assert first["retained_count"] == 1
    assert deleted == [("documents", "life.bin")]
    row = next(a for a in db_mod.list_document_artifacts(doc) if a["artifact_type"] == "original_upload")
    assert row.get("purged_at")

    deleted.clear()
    second = asyncio.run(documents.purge_document_artifacts(doc, member(A, "admin"), apply=True))
    assert second["purged_count"] == 0
    assert second["already_purged_count"] == 1
    assert deleted == []


def test_purge_artifacts_refuses_live_document(doc):
    assert http_status(documents.purge_document_artifacts(doc, member(A, "admin"), apply=False)) == 400
