"""Marqo purge scoping (#73).

``documents.workflow_id`` is the PRIMARY KEY; ``document_id`` carries no unique
constraint. Two rows can therefore share a ``document_id`` — the same bytes
uploaded under two filenames is enough, since ``document_id`` is the content
fingerprint while ``workflow_id`` is derived from the path. Purging one document
by ``doc_id`` alone deleted the other's vectors: the other document still looked
healthy in the UI but had stopped being findable.

Two defects are covered here (D2, the ``limit=1000`` purge cap, is pinned in
``test_vector_store.py``):

D1  the purge is not scoped to one document (``doc_id`` only).
D3  (guard, not a defect on main) ``workflow_id`` is only filterable on an index
    that DECLARED it at creation. A structured index created before that field
    existed answers a ``workflow_id:`` filter with HTTP 400, and because the
    purge path is fail-closed that would surface as a 502 and break
    disable/delete outright. The capability probe keeps that from happening.

All Marqo interaction is an in-memory fake. Nothing here touches a live index.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

import pipeline.db as db_mod
from pipeline.ingestion_records import prepare_ingestion_records
from pipeline.auth.jwt import claims_to_user
from pipeline.routers import documents
from pipeline.services import indexes


def _run(coro):
    return asyncio.run(coro)


def _admin_in(instance: str):
    return claims_to_user({"sub": "tadmin", "tenant_roles": {instance: ["admin"]}})


# =============================================================================
# Fakes
# =============================================================================


class _FakeIndex:
    """In-memory Marqo index mimicking the structured-index filter contract.

    ``fields`` is the set of names the index advertises as filterable. Filtering
    on anything else raises the way Marqo does (HTTP 400 / "no filterable
    field"), which is what D3 guards against.
    """

    def __init__(self, records: list[dict], fields: set[str] | None = None):
        self.records = records
        self.fields = (
            fields
            if fields is not None
            else {"doc_id", "workflow_id", "chunk_num", "instance"}
        )
        self.searches: list[str] = []

    def get_settings(self):
        return {
            "allFields": [{"name": name, "type": "text"} for name in sorted(self.fields)]
        }

    def search(self, q="", filter_string="", limit=10, attributes_to_retrieve=None):
        self.searches.append(filter_string)
        if attributes_to_retrieve and "_id" in attributes_to_retrieve:
            # A structured legacy index rejects `_id` here (prod hotfix #55).
            raise RuntimeError("400 Bad Request: `_id` is not a valid attribute")
        wanted = dict(
            term.split(":", 1) for term in filter_string.split(" AND ") if ":" in term
        )
        for field in wanted:
            if field not in self.fields:
                raise RuntimeError(
                    f"400 Bad Request: Index has no filterable field {field}"
                )
        hits = [
            record
            for record in self.records
            if all(
                str(record.get(field, "")) == value for field, value in wanted.items()
            )
        ]
        keep = list(attributes_to_retrieve or []) + ["_id"]
        return {"hits": [{k: hit[k] for k in keep if k in hit} for hit in hits[:limit]]}

    def delete_documents(self, ids):
        self.records[:] = [r for r in self.records if r["_id"] not in set(ids)]


def _install(monkeypatch, index: _FakeIndex):
    fake = type(
        "m",
        (),
        {"Client": lambda url: type("c", (), {"index": lambda self, name: index})()},
    )
    monkeypatch.setitem(sys.modules, "marqo", fake)


def _records(document_id: str, workflow_id: str | None, texts: list[str]) -> list[dict]:
    return prepare_ingestion_records(
        document_id,
        "doc.pdf",
        [
            {"chunk_number": n, "original_text": text}
            for n, text in enumerate(texts, start=1)
        ],
        workflow_id=workflow_id,
    )


def _two_documents_one_doc_id() -> list[dict]:
    """Two live documents sharing one ``document_id``, both workflow-stamped.

    The reachable shape on main: same bytes (same fingerprint -> same
    ``document_id``) uploaded under two names, so two rows and two workflow ids.
    """
    return _records("shared-doc", "wf-first", ["first one", "first two", "first three"]) + _records(
        "shared-doc", "wf-second", ["second one", "second two"]
    )


# =============================================================================
# D1 — the purge deletes a co-resident document's records
# =============================================================================


@pytest.fixture
def two_aliased_documents(db_connection, monkeypatch):
    """The precondition, built through the ordinary DB layer.

    ``documents.document_id`` has no unique constraint, so this is accepted —
    that is the whole bug. Both rows live in one tenant, hence one physical
    index.
    """
    db_mod.create_tenant_row("tenant-a", display_name="Tenant A")
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    for workflow_id, filename in (("wf-first", "a.pdf"), ("wf-second", "b.pdf")):
        db_mod.upsert_document(
            document_id="shared-doc",
            workflow_id=workflow_id,
            filename=filename,
            filepath=f"/tmp/{filename}",
            stage="completed",
            instance="tenant-a",
            chunk_count=1,
        )
    rows = [
        r
        for r in db_mod.list_documents(limit=100)
        if r.get("document_id") == "shared-doc"
    ]
    assert len(rows) == 2, "precondition: two rows share one document_id"
    return rows


def test_disabling_one_document_does_not_purge_its_alias(
    two_aliased_documents, monkeypatch
):
    """The reported symptom, end to end through the route.

    Disabling ``wf-second`` used to sweep every record under the shared
    ``doc_id`` — including ``wf-first``'s three chunks. ``wf-first`` stays
    enabled in the UI and silently stops being findable.
    """
    index = _FakeIndex(_two_documents_one_doc_id())
    _install(monkeypatch, index)

    result = _run(
        documents.disable_document("wf-second", _admin_in("tenant-a"), remove_from_search=True)
    )

    assert result["marqo_deleted"] == 2, "purged more than the document being disabled"
    surviving = sorted(r["workflow_id"] for r in index.records)
    assert surviving == ["wf-first"] * 3, (
        "disabling one document deleted its alias's vectors"
    )


def test_query_disable_purge_is_scoped_to_one_document(
    two_aliased_documents, monkeypatch
):
    """Same defect on the other purge route (`Include for queries` off)."""
    from pipeline.models import DocumentQueryEnabledUpdate

    index = _FakeIndex(_two_documents_one_doc_id())
    _install(monkeypatch, index)

    _run(
        documents.set_document_query_enabled(
            "wf-first",
            DocumentQueryEnabledUpdate(query_enabled=False),
            _admin_in("tenant-a"),
        )
    )

    surviving = sorted(r["workflow_id"] for r in index.records)
    assert surviving == ["wf-second"] * 2, (
        "turning off queries for one document deleted its alias's vectors"
    )


def test_already_purged_document_is_benign_when_nothing_shares_its_doc_id(monkeypatch):
    """A scoped miss is ambiguous only when strays exist; otherwise it is a
    no-op. Re-purging an already-purged document is not a failure."""
    index = _FakeIndex(_records("solo-doc", "wf-solo", ["one"]))
    _install(monkeypatch, index)

    first = indexes.delete_chunks_from_marqo(
        "solo-doc", index_name="t-tenant-a-vet", workflow_id="wf-solo"
    )
    assert first["deleted"] == 1

    again = indexes.delete_chunks_from_marqo(
        "solo-doc", index_name="t-tenant-a-vet", workflow_id="wf-solo"
    )
    assert again["deleted"] == 0
    assert "error" not in again

    one = indexes.delete_single_chunk_from_marqo(
        "solo-doc", 1, index_name="t-tenant-a-vet", workflow_id="wf-solo"
    )
    assert one["deleted"] is False and one.get("reason") == "not_found"
    assert "error" not in one


# =============================================================================
# D3 — an index that never declared workflow_id must not 400
# =============================================================================


def _legacy_index() -> _FakeIndex:
    """A structured index created before ``workflow_id`` joined the schema.

    A filterable field cannot be added to a structured index in place, so this
    shape is permanent for indexes already in service.
    """
    records = _records("only-doc", "wf-live", ["one", "two"])
    for record in records:
        record.pop("workflow_id", None)
    return _FakeIndex(records, fields={"doc_id", "chunk_num", "instance"})


def test_single_chunk_purge_degrades_on_an_index_without_a_workflow_id_field(monkeypatch):
    index = _legacy_index()
    _install(monkeypatch, index)

    result = indexes.delete_single_chunk_from_marqo(
        "only-doc", 1, index_name="legacy-vet-index", workflow_id="wf-live"
    )

    assert result.get("deleted") is True, f"purge 400'd on a legacy index: {result}"
    assert [r["chunk_num"] for r in index.records] == [2]
    assert all("workflow_id" not in f for f in index.searches)
