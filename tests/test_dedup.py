"""Upload deduplication (#43).

Contract: the same file uploaded into the same tenant AND index returns HTTP 200
with the EXISTING document and ``duplicate=True`` and starts NO new pipeline
(duplicates are not rejected). ``force=true`` bypasses the check and re-ingests
under its own workflow id AND its own document_id. The same file in a DIFFERENT
tenant — or a different index of the same tenant — is allowed. A soft-deleted
match is NOT a duplicate: it neither blocks the re-ingest nor invites a restore.

Route handlers are invoked directly (the tenant-isolation suite's style): the app
lifespan needs a live Temporal connection, so the functions are called with
explicit Query/UploadFile args and the module clients mocked.
"""

from __future__ import annotations

import asyncio
import sys
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile
from temporalio.exceptions import WorkflowAlreadyStartedError

import pipeline.api as api
import pipeline.db as db_mod
from pipeline.activities import prepare_ingestion_records
from pipeline.auth.jwt import claims_to_user


def _run(coro):
    return asyncio.run(coro)


PDF_BYTES = b"%PDF-1.4 minimal dedup test body"
A = "tenant-a"
B = "tenant-b"


def _curator_in(instance: str):
    return claims_to_user({"sub": "cur", "tenant_roles": {instance: ["content_curator"]}})


def _upload_file(content: bytes = PDF_BYTES, filename: str = "doc.pdf") -> UploadFile:
    return UploadFile(
        BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": "application/pdf"}),
    )


@pytest.fixture
def wired(db_connection, monkeypatch):
    """api bound to the test db with mocked Temporal + MinIO clients."""
    monkeypatch.setattr(api, "db", db_mod)
    temporal = AsyncMock()
    handle = AsyncMock()
    handle.query = AsyncMock(return_value={"stage": "registered"})
    temporal.get_workflow_handle = MagicMock(return_value=handle)
    temporal.start_workflow = AsyncMock(return_value=handle)
    monkeypatch.setattr(api, "temporal_client", temporal)
    monkeypatch.setattr(api, "minio_client", MagicMock())
    # Direct handler calls shouldn't trip the IP rate limiter.
    monkeypatch.setattr(api.limiter, "enabled", False)
    # Reset the module's one-shot Temporal search-attribute feature flag.
    api._instance_search_attr_supported = None
    return temporal


def _upload(
    user,
    *,
    instance: str,
    force: bool = False,
    content: bytes = PDF_BYTES,
    index_name: str = "documents-index",
):
    return _run(
        api.upload_and_process(
            MagicMock(),  # request (rate limiter disabled in tests)
            user,
            file=_upload_file(content=content),
            auto_approve=True,
            instance=instance,
            index_name=index_name,
            force=force,
        )
    )


class _FakeMarqoIndex:
    """Minimal in-memory Marqo index understanding the purge filter strings."""

    def __init__(self, records: list[dict]):
        self.records = records

    def get_settings(self):
        # A modern structured index: workflow_id was declared at creation, so the
        # scoped purge filter is answerable here. See tests/test_marqo_doc_scope.py
        # for the legacy index that never declared it.
        return {
            "allFields": [
                {"name": name, "type": "text"}
                for name in ("doc_id", "workflow_id", "chunk_num", "instance")
            ]
        }

    def search(self, q="", filter_string="", limit=10, attributes_to_retrieve=None):
        wanted = dict(
            term.split(":", 1) for term in filter_string.split(" AND ") if ":" in term
        )
        hits = [
            record
            for record in self.records
            if all(str(record.get(field, "")) == value for field, value in wanted.items())
        ]
        keep = list(attributes_to_retrieve or ["_id"]) + ["_id"]
        return {"hits": [{k: hit[k] for k in keep if k in hit} for hit in hits[:limit]]}

    def delete_documents(self, ids):
        self.records[:] = [r for r in self.records if r["_id"] not in set(ids)]


def _install_fake_marqo(monkeypatch, index: _FakeMarqoIndex):
    fake = type("m", (), {"Client": lambda url: type("c", (), {"index": lambda self, name: index})()})
    monkeypatch.setitem(sys.modules, "marqo", fake)


# --- db layer ----------------------------------------------------------------


def test_find_document_by_fingerprint_is_tenant_scoped(db_connection):
    db = db_connection
    db.upsert_document(
        workflow_id="wf-a",
        document_id="d-a",
        filename="a.pdf",
        filepath="/tmp/a.pdf",
        stage="completed",
        instance=A,
        source_file_fingerprint="fp-123",
    )
    found = db.find_document_by_fingerprint(A, "fp-123")
    assert found is not None and found["workflow_id"] == "wf-a"
    # Same fingerprint, different tenant -> not a duplicate.
    assert db.find_document_by_fingerprint(B, "fp-123") is None
    # Unknown fingerprint / empty input -> None.
    assert db.find_document_by_fingerprint(A, "nope") is None
    assert db.find_document_by_fingerprint(A, "") is None


def test_find_document_by_fingerprint_is_index_scoped(db_connection):
    """Same file in the tenant's `vet` index is not a duplicate for `general`."""
    db = db_connection
    db.upsert_document(
        workflow_id="wf-vet",
        document_id="d-vet",
        filename="ref.pdf",
        filepath="/tmp/ref.pdf",
        stage="completed",
        instance=A,
        index="vet",
        source_file_fingerprint="fp-idx",
    )
    assert db.find_document_by_fingerprint(A, "fp-idx", index="vet")["workflow_id"] == "wf-vet"
    # A different index of the SAME tenant -> free to ingest.
    assert db.find_document_by_fingerprint(A, "fp-idx", index="general") is None
    # The tenant default (NULL index) is its own bucket too.
    assert db.find_document_by_fingerprint(A, "fp-idx") is None


def test_find_document_by_fingerprint_skips_soft_deleted(db_connection):
    """A soft-deleted doc must not block (or be offered for restore on) a re-ingest."""
    db = db_connection
    db.upsert_document(
        workflow_id="wf-gone",
        document_id="d-gone",
        filename="gone.pdf",
        filepath="/tmp/gone.pdf",
        stage="completed",
        instance=A,
        source_file_fingerprint="fp-gone",
    )
    db.set_document_disabled("wf-gone", True)
    assert db.find_document_by_fingerprint(A, "fp-gone") is None
    # The upload path still needs to see it: its document_id owns the fingerprint.
    assert db.find_document_by_fingerprint(A, "fp-gone", include_disabled=True) is not None


# --- upload dedup path -------------------------------------------------------


def test_duplicate_same_tenant_returns_existing_and_starts_no_workflow(wired):
    temporal = wired
    user = _curator_in(A)

    first = _upload(user, instance=A)
    assert first.duplicate is False
    assert temporal.start_workflow.await_count == 1

    second = _upload(user, instance=A)
    assert second.duplicate is True
    assert second.workflow_id == first.workflow_id
    # No second pipeline was started.
    assert temporal.start_workflow.await_count == 1


def test_force_bypasses_dedup_and_reingests(wired):
    temporal = wired
    user = _curator_in(A)

    _upload(user, instance=A)
    assert temporal.start_workflow.await_count == 1

    forced = _upload(user, instance=A, force=True)
    assert forced.duplicate is False
    # A fresh run was started despite the identical file.
    assert temporal.start_workflow.await_count == 2


def test_same_file_different_tenant_is_allowed(wired):
    temporal = wired

    _upload(_curator_in(A), instance=A)
    assert temporal.start_workflow.await_count == 1

    # Identical bytes into tenant-b -> not a duplicate, a new run starts.
    other = _upload(_curator_in(B), instance=B)
    assert other.duplicate is False
    assert temporal.start_workflow.await_count == 2


def test_soft_deleted_match_reingests_instead_of_offering_restore(wired):
    """A disabled doc was deleted for a reason: re-upload ingests fresh, no force needed."""
    temporal = wired
    user = _curator_in(A)
    first = _upload(user, instance=A)
    db_mod.set_document_disabled(first.workflow_id, True)

    again = _upload(user, instance=A)
    assert again.duplicate is False
    assert again.workflow_id != first.workflow_id
    assert temporal.start_workflow.await_count == 2
    # The disabled row still owns the fingerprint as its document_id, so the new
    # run must not key its vector records off the same value.
    assert again.document_id != first.document_id
    assert again.canonical_document_id == first.canonical_document_id


def test_same_file_in_two_indexes_is_not_a_duplicate(wired):
    """Ingesting one reference doc into `vet` and `general` must not need force (#43)."""
    temporal = wired
    user = _curator_in(A)
    db_mod.create_tenant_row(A, display_name="Tenant A")
    db_mod.create_index_row(A, "vet", "t-tenant-a-vet", is_default=True)
    db_mod.create_index_row(A, "general", "t-tenant-a-general")

    first = _upload(user, instance=A, index_name="t-tenant-a-vet")
    assert first.duplicate is False

    other = _upload(user, instance=A, index_name="t-tenant-a-general")
    assert other.duplicate is False
    assert temporal.start_workflow.await_count == 2

    # ...but the same file into the same index still dedups.
    dup = _upload(user, instance=A, index_name="t-tenant-a-vet")
    assert dup.duplicate is True
    assert dup.workflow_id == first.workflow_id
    assert temporal.start_workflow.await_count == 2


def test_default_index_addressed_two_ways_is_one_dedup_bucket(wired):
    """The tenant default index is reachable by BOTH its logical name and the
    unregistered/None default, and both land in the SAME physical Marqo index.

    Dedup keyed on the raw logical name therefore missed, and a second row was
    created carrying the SAME fingerprint-derived ``document_id`` inside one
    physical index — the aliasing that makes a ``doc_id``-only purge unsafe. No
    ``force`` involved; this is the ordinary upload path.
    """
    temporal = wired
    user = _curator_in(A)
    db_mod.create_tenant_row(A, display_name="Tenant A")
    db_mod.create_index_row(A, "vet", "t-tenant-a-vet", is_default=True)

    first = _upload(user, instance=A, index_name="t-tenant-a-vet")
    assert first.duplicate is False

    # `documents-index` is not registered -> "the tenant default" -> resolves to
    # the very same physical index as `vet`.
    second = _upload(user, instance=A, index_name="documents-index")
    assert second.duplicate is True, "the same file aliased into one physical index"
    assert second.workflow_id == first.workflow_id
    assert temporal.start_workflow.await_count == 1

    # No two rows may share a document_id inside one physical index.
    rows = db_mod.list_documents(include_disabled=True, instances=[A])
    seen: dict[tuple, str] = {}
    for row in rows:
        physical = db_mod.resolve_marqo_index(A, row.get("index"))
        key = (physical, row["document_id"])
        assert key not in seen, f"document_id aliased in {physical}"
        seen[key] = row["workflow_id"]


def test_non_default_named_index_still_dedups_separately(wired):
    """Collapsing the default onto None must not merge genuinely distinct indexes."""
    temporal = wired
    user = _curator_in(A)
    db_mod.create_tenant_row(A, display_name="Tenant A")
    db_mod.create_index_row(A, "vet", "t-tenant-a-vet", is_default=True)
    db_mod.create_index_row(A, "general", "t-tenant-a-general")

    _upload(user, instance=A, index_name="t-tenant-a-vet")
    other = _upload(user, instance=A, index_name="t-tenant-a-general")
    assert other.duplicate is False
    assert temporal.start_workflow.await_count == 2


# --- forced rerun ids --------------------------------------------------------


def test_two_forced_ingests_in_the_same_second_do_not_collide(wired, monkeypatch):
    """Regression: `-rerun-<int(time.time())>` reused the id and 500'd (#43).

    Time is frozen so both forced runs land in the same second — exactly what a
    double-click on "Force new run" does.
    """
    temporal = wired
    user = _curator_in(A)
    monkeypatch.setattr(api.time, "time", lambda: 1_700_000_000.0)

    _upload(user, instance=A)
    forced_one = _upload(user, instance=A, force=True)
    forced_two = _upload(user, instance=A, force=True)

    ids = {forced_one.workflow_id, forced_two.workflow_id}
    assert len(ids) == 2, "forced reruns in the same second shared a workflow id"
    assert temporal.start_workflow.await_count == 3
    # And a genuinely duplicate id is an actionable 409, never an unhandled 500.
    temporal.start_workflow.side_effect = WorkflowAlreadyStartedError("wf", "DocumentPipelineWorkflow")
    with pytest.raises(HTTPException) as exc:
        _upload(user, instance=A, force=True)
    assert exc.value.status_code == 409


def test_forced_rerun_gets_its_own_document_id(wired):
    """Two rows for one file must not share vector records (#43)."""
    user = _curator_in(A)
    first = _upload(user, instance=A)
    forced = _upload(user, instance=A, force=True)

    assert forced.document_id != first.document_id
    # Both still point back at the same source file.
    assert forced.canonical_document_id == first.canonical_document_id == first.document_id

    # Marqo record identity is derived from document_id, so the ids no longer alias.
    chunks = [{"chunk_number": 1, "original_text": "same text in both runs"}]
    a_records = prepare_ingestion_records(first.document_id, "doc.pdf", chunks, workflow_id=first.workflow_id)
    b_records = prepare_ingestion_records(forced.document_id, "doc.pdf", chunks, workflow_id=forced.workflow_id)
    assert a_records[0]["_id"] != b_records[0]["_id"]
    assert a_records[0]["doc_id"] != b_records[0]["doc_id"]


def test_purging_one_document_leaves_the_other_intact(wired, monkeypatch):
    """Purges are scoped by workflow_id, so one document cannot blank another."""
    user = _curator_in(A)
    first = _upload(user, instance=A)
    forced = _upload(user, instance=A, force=True)

    chunks = [
        {"chunk_number": 1, "original_text": "chunk one"},
        {"chunk_number": 2, "original_text": "chunk two"},
    ]
    records = prepare_ingestion_records(
        first.document_id, "doc.pdf", chunks, workflow_id=first.workflow_id
    ) + prepare_ingestion_records(
        forced.document_id, "doc.pdf", chunks, workflow_id=forced.workflow_id
    )
    index = _FakeMarqoIndex(records)
    _install_fake_marqo(monkeypatch, index)

    # Disabling one chunk of the first document leaves the second document whole.
    api.delete_single_chunk_from_marqo(
        first.document_id, 1, index_name="documents-index", workflow_id=first.workflow_id
    )
    surviving = [r for r in index.records if r["workflow_id"] == forced.workflow_id]
    assert len(surviving) == 2
    assert len([r for r in index.records if r["workflow_id"] == first.workflow_id]) == 1

    # Deleting the first document entirely still leaves the second searchable.
    api.delete_chunks_from_marqo(
        first.document_id, index_name="documents-index", workflow_id=first.workflow_id
    )
    assert [r["workflow_id"] for r in index.records] == [forced.workflow_id] * 2


def test_purge_is_scoped_by_workflow_id_when_doc_ids_already_alias(monkeypatch):
    """Rows written BEFORE this fix share a document_id; workflow_id keeps them apart."""
    old_chunks = [
        {"chunk_number": 1, "original_text": "old chunk one"},
        {"chunk_number": 2, "original_text": "old chunk two"},
    ]
    # Re-OCR'd text, so the record ids differ while doc_id still aliases.
    new_chunks = [
        {"chunk_number": 1, "original_text": "new chunk one"},
        {"chunk_number": 2, "original_text": "new chunk two"},
    ]
    records = prepare_ingestion_records("shared-doc", "doc.pdf", old_chunks, workflow_id="wf-old") + (
        prepare_ingestion_records("shared-doc", "doc.pdf", new_chunks, workflow_id="wf-new")
    )
    index = _FakeMarqoIndex(records)
    _install_fake_marqo(monkeypatch, index)

    api.delete_single_chunk_from_marqo(
        "shared-doc", 1, index_name="documents-index", workflow_id="wf-new"
    )
    assert sorted(r["chunk_num"] for r in index.records if r["workflow_id"] == "wf-old") == [1, 2]

    api.delete_chunks_from_marqo("shared-doc", index_name="documents-index", workflow_id="wf-new")
    assert [r["workflow_id"] for r in index.records] == ["wf-old"] * 2


def test_scoped_miss_over_legacy_records_fails_closed(monkeypatch):
    """Was `test_purge_falls_back_for_records_without_a_workflow_id`.

    That test asserted the unscoped RETRY (removed as PR #53 blocker B1) — and it
    only looked safe because the fixture held nothing but legacy records. Give it
    a co-resident document and the same retry deletes the wrong one. The retry is
    gone: a scoped miss with strays under the doc_id now fails closed. See
    tests/test_marqo_doc_scope.py for the two-document proof.
    """
    chunks = [{"chunk_number": 1, "original_text": "legacy chunk"}]
    records = prepare_ingestion_records("legacy-doc", "doc.pdf", chunks, workflow_id=None)
    assert records[0]["workflow_id"] == ""
    index = _FakeMarqoIndex(records)
    _install_fake_marqo(monkeypatch, index)

    result = api.delete_chunks_from_marqo(
        "legacy-doc", index_name="documents-index", workflow_id="wf-any"
    )
    assert result["deleted"] == 0
    assert "refusing to purge" in result.get("error", "")
    assert len(index.records) == 1
