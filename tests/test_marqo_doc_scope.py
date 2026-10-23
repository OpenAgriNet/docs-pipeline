"""Marqo purge scoping through the routes (#73).

``documents.workflow_id`` is the primary key and ``document_id`` (the content
fingerprint) is not unique, so the same bytes uploaded under two names give two
rows sharing one ``document_id``. Purging one by ``doc_id`` alone deleted the
other's vectors: it still looked healthy in the UI but stopped being findable.

D1  the purge must be scoped to one document, not ``doc_id`` only.
D3  ``workflow_id`` is filterable only on an index that declared it at creation;
    on an older index a ``workflow_id:`` filter 400s, which the fail-closed purge
    would turn into a 502 on every disable/delete.
(D2, the ``limit=1000`` purge cap, is pinned in ``test_vector_store.py``.)
"""

from __future__ import annotations

import asyncio

import pytest

import pipeline.db as db_mod
from pipeline.ingestion_records import prepare_ingestion_records
from pipeline.models import DocumentQueryEnabledUpdate
from pipeline.routers import documents
from pipeline.services import indexes
from tests.helpers import member, seed_document

INDEX = "t-tenant-a-vet"


def _records(document_id: str, workflow_id: str, texts: list[str]) -> list[dict]:
    chunks = [{"chunk_number": n, "original_text": text} for n, text in enumerate(texts, start=1)]
    return prepare_ingestion_records(document_id, "doc.pdf", chunks, workflow_id=workflow_id)


@pytest.fixture
def aliased_index(db_connection, fake_marqo):
    """Two live rows sharing ``document_id`` in one tenant, and their shared index.

    The schema accepts this (no unique constraint), which is the whole bug.
    """
    db_mod.create_tenant_row("tenant-a", display_name="Tenant A")
    db_mod.create_index_row("tenant-a", "vet", INDEX, is_default=True)
    for workflow_id, filename in (("wf-first", "a.pdf"), ("wf-second", "b.pdf")):
        seed_document(
            db_connection, workflow_id, document_id="shared-doc", filename=filename,
            stage="completed", instance="tenant-a", chunk_count=1,
        )
    rows = [r for r in db_mod.list_documents(limit=100) if r.get("document_id") == "shared-doc"]
    assert len(rows) == 2, "precondition: two rows share one document_id"
    return fake_marqo.add_index(
        INDEX,
        _records("shared-doc", "wf-first", ["first one", "first two", "first three"])
        + _records("shared-doc", "wf-second", ["second one", "second two"]),
    )


def test_disabling_one_document_does_not_purge_its_alias(aliased_index):
    result = asyncio.run(
        documents.disable_document("wf-second", member("tenant-a", "admin"), remove_from_search=True)
    )
    assert result["marqo_deleted"] == 2, "purged more than the document being disabled"
    assert sorted(r["workflow_id"] for r in aliased_index.records) == ["wf-first"] * 3, (
        "disabling one document deleted its alias's vectors"
    )


def test_query_disable_purge_is_scoped_to_one_document(aliased_index):
    """Same defect on the other purge route (`Include for queries` off)."""
    asyncio.run(documents.set_document_query_enabled(
        "wf-first", DocumentQueryEnabledUpdate(query_enabled=False), member("tenant-a", "admin"),
    ))
    assert sorted(r["workflow_id"] for r in aliased_index.records) == ["wf-second"] * 2, (
        "turning off queries for one document deleted its alias's vectors"
    )


def test_already_purged_document_is_benign_when_nothing_shares_its_doc_id(fake_marqo):
    """A scoped miss is ambiguous only when strays exist; otherwise re-purging is a no-op."""
    fake_marqo.add_index(INDEX, _records("solo-doc", "wf-solo", ["one"]))
    assert indexes.delete_chunks_from_marqo("solo-doc", index_name=INDEX, workflow_id="wf-solo")["deleted"] == 1

    again = indexes.delete_chunks_from_marqo("solo-doc", index_name=INDEX, workflow_id="wf-solo")
    assert again["deleted"] == 0
    assert "error" not in again

    one = indexes.delete_single_chunk_from_marqo("solo-doc", 1, index_name=INDEX, workflow_id="wf-solo")
    assert one["deleted"] is False and one.get("reason") == "not_found"
    assert "error" not in one


def test_single_chunk_purge_degrades_on_an_index_without_a_workflow_id_field(fake_marqo):
    """D3. A filterable field cannot be added in place, so this shape is permanent."""
    records = _records("only-doc", "wf-live", ["one", "two"])
    for record in records:
        record.pop("workflow_id", None)
    index = fake_marqo.add_index("legacy-vet-index", records, fields={"doc_id", "chunk_num", "instance"})

    result = indexes.delete_single_chunk_from_marqo(
        "only-doc", 1, index_name="legacy-vet-index", workflow_id="wf-live"
    )

    assert result.get("deleted") is True, f"purge 400'd on a legacy index: {result}"
    assert [r["chunk_num"] for r in index.records] == [2]
    assert all("workflow_id" not in f for f in index.filters)
