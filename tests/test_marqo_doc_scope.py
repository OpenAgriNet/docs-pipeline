"""Marqo purge scoping (#73).

``documents.workflow_id`` is the PRIMARY KEY; ``document_id`` carries no unique
constraint. Two rows can therefore share a ``document_id`` — the same bytes
uploaded under two filenames is enough, since ``document_id`` is the content
fingerprint while ``workflow_id`` is derived from the path. Purging one document
by ``doc_id`` alone deleted the other's vectors: the other document still looked
healthy in the UI but had stopped being findable.

Three defects are covered here:

D1  the purge is not scoped to one document (``doc_id`` only).
D2  the search is capped at ``limit=1000``, so a longer document reports a
    successful purge while leaving its tail searchable.
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

import pipeline.api as api
import pipeline.db as db_mod
from pipeline.activities import prepare_ingestion_records
from pipeline.auth.jwt import claims_to_user
from unittest.mock import MagicMock


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
    monkeypatch.setattr(api, "db", db_mod)
    monkeypatch.setattr(api, "temporal_client", MagicMock())
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
        api.disable_document("wf-second", _admin_in("tenant-a"), remove_from_search=True)
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
        api.set_document_query_enabled(
            "wf-first",
            DocumentQueryEnabledUpdate(query_enabled=False),
            _admin_in("tenant-a"),
        )
    )

    surviving = sorted(r["workflow_id"] for r in index.records)
    assert surviving == ["wf-second"] * 2, (
        "turning off queries for one document deleted its alias's vectors"
    )


def test_single_chunk_purge_is_scoped_to_one_document(monkeypatch):
    """Chunk 3 belongs to ``wf-first`` only; purging ``wf-second``'s chunk 3
    must not reach it."""
    index = _FakeIndex(_two_documents_one_doc_id())
    _install(monkeypatch, index)

    result = api.delete_single_chunk_from_marqo(
        "shared-doc", 3, index_name="t-tenant-a-vet", workflow_id="wf-second"
    )

    assert result["deleted"] is False
    assert len(index.records) == 5, "single-chunk purge deleted another document's chunk"


def test_scoped_miss_over_unattributable_records_fails_closed(monkeypatch):
    """Ambiguity must raise, not guess.

    ``wf-legacy`` predates the ``workflow_id`` stamp, so its records carry
    ``workflow_id:""``. Index state for "these ownerless records are mine" and
    for "I am already purged and these belong to a co-resident document" is
    byte-identical — no rule can serve both. Guessing wrong deletes another
    document's vectors irreversibly, so we refuse.
    """
    index = _FakeIndex(
        _records("shared-doc", None, ["legacy one", "legacy two"])
        + _records("shared-doc", "wf-second", ["second one"])
    )
    _install(monkeypatch, index)

    result = api.delete_chunks_from_marqo(
        "shared-doc", index_name="t-tenant-a-vet", workflow_id="wf-legacy"
    )

    assert result["deleted"] == 0
    assert "refusing to purge" in result.get("error", "")
    assert len(index.records) == 3, "a document that may not own these records purged them"


def test_already_purged_document_is_benign_when_nothing_shares_its_doc_id(monkeypatch):
    """A scoped miss is ambiguous only when strays exist; otherwise it is a
    no-op. Re-purging an already-purged document is not a failure."""
    index = _FakeIndex(_records("solo-doc", "wf-solo", ["one"]))
    _install(monkeypatch, index)

    first = api.delete_chunks_from_marqo(
        "solo-doc", index_name="t-tenant-a-vet", workflow_id="wf-solo"
    )
    assert first["deleted"] == 1

    again = api.delete_chunks_from_marqo(
        "solo-doc", index_name="t-tenant-a-vet", workflow_id="wf-solo"
    )
    assert again["deleted"] == 0
    assert "error" not in again

    one = api.delete_single_chunk_from_marqo(
        "solo-doc", 1, index_name="t-tenant-a-vet", workflow_id="wf-solo"
    )
    assert one["deleted"] is False and one.get("reason") == "not_found"
    assert "error" not in one


# =============================================================================
# D2 — limit=1000 silently truncated the purge
# =============================================================================


def test_bulk_purge_is_not_capped_at_one_thousand_records(monkeypatch):
    """A 1,400-chunk document reported ``deleted: 1000`` and left 400 chunks
    searchable behind a document the UI shows as removed."""
    index = _FakeIndex(_records("big-doc", "wf-big", [f"chunk {n}" for n in range(1400)]))
    _install(monkeypatch, index)
    assert len(index.records) == 1400

    result = api.delete_chunks_from_marqo(
        "big-doc", index_name="t-tenant-a-vet", workflow_id="wf-big"
    )

    assert result["deleted"] == 1400, "purge truncated at the search page size"
    assert index.records == [], "chunks left searchable after a 'successful' purge"


def test_truncated_purge_also_fixed_on_the_unscoped_path(monkeypatch):
    """The cap is a defect independent of scoping — it bites on a legacy index
    that cannot answer a ``workflow_id`` filter too."""
    records = _records("big-doc", "wf-big", [f"chunk {n}" for n in range(1200)])
    for record in records:
        record.pop("workflow_id", None)
    index = _FakeIndex(records, fields={"doc_id", "chunk_num", "instance"})
    _install(monkeypatch, index)

    result = api.delete_chunks_from_marqo(
        "big-doc", index_name="legacy-vet-index", workflow_id="wf-big"
    )

    assert result["deleted"] == 1200
    assert index.records == []


def test_purge_loop_terminates_when_delete_does_not_take(monkeypatch):
    """Safety valve: a delete that silently no-ops must not spin forever."""

    class _Stubborn(_FakeIndex):
        def delete_documents(self, ids):
            return {"status": "ok"}

    index = _Stubborn(_records("stuck-doc", "wf-stuck", ["one", "two"]))
    _install(monkeypatch, index)

    result = api.delete_chunks_from_marqo(
        "stuck-doc", index_name="t-tenant-a-vet", workflow_id="wf-stuck"
    )
    assert result["deleted"] == 2


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


def test_bulk_purge_degrades_on_an_index_without_a_workflow_id_field(monkeypatch):
    """Without the capability probe this 400s, and the fail-closed purge 502s —
    disable/delete would be broken outright on the live legacy index."""
    index = _legacy_index()
    _install(monkeypatch, index)

    result = api.delete_chunks_from_marqo(
        "only-doc", index_name="legacy-vet-index", workflow_id="wf-live"
    )

    assert "error" not in result, f"purge 400'd on a workflow_id-less index: {result}"
    assert result["deleted"] == 2
    assert index.records == []
    assert all("workflow_id" not in f for f in index.searches)


def test_single_chunk_purge_degrades_on_an_index_without_a_workflow_id_field(monkeypatch):
    index = _legacy_index()
    _install(monkeypatch, index)

    result = api.delete_single_chunk_from_marqo(
        "only-doc", 1, index_name="legacy-vet-index", workflow_id="wf-live"
    )

    assert result.get("deleted") is True, f"purge 400'd on a legacy index: {result}"
    assert [r["chunk_num"] for r in index.records] == [2]
    assert all("workflow_id" not in f for f in index.searches)


def test_index_has_workflow_id_field_probe():
    """Mirrors ``_index_has_instance_field`` except on the error branch.

    A probe FAILURE returns ``True`` (keep the narrow, scoped filter). Guessing
    "absent" would silently widen every purge on a transient Marqo hiccup; if
    the field really is missing the search 400s and the purge fails closed,
    which is the intended outcome for an unexplained error.
    """
    assert api._index_has_workflow_id_field(_FakeIndex([])) is True
    assert api._index_has_workflow_id_field(_legacy_index()) is False

    class _Unreachable:
        def get_settings(self):
            raise RuntimeError("connection refused")

    assert api._index_has_workflow_id_field(_Unreachable()) is True
    assert api._index_has_instance_field(_Unreachable()) is False, (
        "the asymmetry with the instance probe is deliberate, not drift"
    )
