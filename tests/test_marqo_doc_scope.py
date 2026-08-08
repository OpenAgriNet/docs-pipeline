"""Marqo purge scoping — PR #53 blockers B1 and B3.

B1: the workflow_id-scoped purge used to RETRY with the unscoped ``doc_id``-only
    filter whenever the scoped search matched nothing. ``doc_id`` is not unique,
    so that retry deleted a co-resident document's records.

B3: ``workflow_id`` is only filterable on an index that DECLARED it at creation.
    A structured index created before that field existed answers a
    ``workflow_id:`` filter with HTTP 400, and because the purge path is
    fail-closed that surfaced as a 502 and broke disable/delete outright.
"""

from __future__ import annotations

import sys

import pipeline.api as api
from pipeline.activities import prepare_ingestion_records


# =============================================================================
# Fakes
# =============================================================================


class _FakeIndex:
    """In-memory Marqo index that mimics the structured-index filter contract.

    ``fields`` is the set of names the index advertises as filterable. Filtering
    on anything else raises the way Marqo does (HTTP 400 / "has no filterable
    field"), which is the whole of B3.
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
        return {"allFields": [{"name": name, "type": "text"} for name in sorted(self.fields)]}

    def search(self, q="", filter_string="", limit=10, attributes_to_retrieve=None):
        self.searches.append(filter_string)
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
            if all(str(record.get(field, "")) == value for field, value in wanted.items())
        ]
        keep = list(attributes_to_retrieve or ["_id"]) + ["_id"]
        return {"hits": [{k: hit[k] for k in keep if k in hit} for hit in hits[:limit]]}

    def delete_documents(self, ids):
        self.records[:] = [r for r in self.records if r["_id"] not in set(ids)]


def _install(monkeypatch, index: _FakeIndex):
    fake = type(
        "m", (), {"Client": lambda url: type("c", (), {"index": lambda self, name: index})()}
    )
    monkeypatch.setitem(sys.modules, "marqo", fake)


def _corpus() -> list[dict]:
    """Two documents sharing one ``document_id``: one legacy, one workflow-stamped.

    The legacy pair carries ``workflow_id:""`` (written before the field was
    stamped); the new pair belongs to ``wf-new``.
    """
    legacy = prepare_ingestion_records(
        "shared-doc",
        "doc.pdf",
        [
            {"chunk_number": 1, "original_text": "legacy one"},
            {"chunk_number": 2, "original_text": "legacy two"},
            {"chunk_number": 3, "original_text": "legacy three"},
        ],
        workflow_id=None,
    )
    assert all(r["workflow_id"] == "" for r in legacy)
    new = prepare_ingestion_records(
        "shared-doc",
        "doc.pdf",
        [
            {"chunk_number": 1, "original_text": "new one"},
            {"chunk_number": 2, "original_text": "new two"},
        ],
        workflow_id="wf-new",
    )
    return legacy + new


# =============================================================================
# B1 — the unscoped retry deleted the co-resident document
# =============================================================================


def test_scoped_bulk_purge_never_falls_back_to_the_unscoped_filter(monkeypatch):
    """Reported symptom: purging one document reported ``deleted: 3`` and emptied
    the index. It happens whenever the scoped search matches nothing — an already
    purged document, or one whose chunks were removed one at a time — because the
    retry then swept every record sharing the ``doc_id``.
    """
    index = _FakeIndex(_corpus())
    _install(monkeypatch, index)

    # wf-new's own records go first; the legacy document is untouched.
    first = api.delete_chunks_from_marqo(
        "shared-doc", index_name="t-a-vet", workflow_id="wf-new"
    )
    assert first["deleted"] == 2
    assert len(index.records) == 3

    # Purging wf-new AGAIN matches nothing of its own, while three records still
    # share the doc_id. The index cannot tell "wf-new's records predate the
    # workflow_id stamp" from "wf-new is already gone and these belong to someone
    # else" — so this must fail closed, never sweep.
    again = api.delete_chunks_from_marqo(
        "shared-doc", index_name="t-a-vet", workflow_id="wf-new"
    )
    assert again["deleted"] == 0
    assert "refusing to purge" in again.get("error", ""), (
        "unscoped retry deleted another document's records"
    )
    assert len(index.records) == 3


def test_scoped_single_chunk_purge_never_falls_back(monkeypatch):
    """wf-new has no chunk 3; the retry used to delete the legacy document's."""
    index = _FakeIndex(_corpus())
    _install(monkeypatch, index)

    result = api.delete_single_chunk_from_marqo(
        "shared-doc", 3, index_name="t-a-vet", workflow_id="wf-new"
    )
    assert result["deleted"] is False
    assert "refusing to purge" in result.get("error", "")
    assert len(index.records) == 5, "unscoped retry deleted another document's chunk"


def test_ambiguous_doc_id_fails_closed_rather_than_guessing(monkeypatch):
    """The mirror case: a legacy row purging against a co-resident stamped one.

    Byte-for-byte the same index state as the double-purge above, which is the
    point — no rule can serve both. We refuse and say so, because the cost of
    guessing wrong is deleting another document's vectors, and the purge path is
    deliberately fail-closed so the caller surfaces a 502 instead of proceeding.
    """
    index = _FakeIndex(_corpus())
    _install(monkeypatch, index)

    result = api.delete_chunks_from_marqo(
        "shared-doc", index_name="t-a-vet", workflow_id="wf-legacy-row"
    )
    assert result["deleted"] == 0
    assert "refusing to purge" in result.get("error", "")
    assert len(index.records) == 5, "a document that may not own these records purged them"


def test_already_purged_document_is_benign_when_nothing_shares_its_doc_id(monkeypatch):
    """A scoped miss is only ambiguous when strays exist; otherwise it is a no-op."""
    index = _FakeIndex(
        prepare_ingestion_records(
            "solo-doc", "doc.pdf", [{"chunk_number": 1, "original_text": "one"}],
            workflow_id="wf-solo",
        )
    )
    _install(monkeypatch, index)

    first = api.delete_chunks_from_marqo("solo-doc", index_name="t-a-vet", workflow_id="wf-solo")
    assert first["deleted"] == 1

    again = api.delete_chunks_from_marqo("solo-doc", index_name="t-a-vet", workflow_id="wf-solo")
    assert again["deleted"] == 0
    assert "error" not in again

    one = api.delete_single_chunk_from_marqo(
        "solo-doc", 1, index_name="t-a-vet", workflow_id="wf-solo"
    )
    assert one["deleted"] is False and one.get("reason") == "not_found"
    assert "error" not in one


# =============================================================================
# B3 — an index that never declared workflow_id must not 400
# =============================================================================


def _legacy_index() -> _FakeIndex:
    """A structured index created before ``workflow_id`` existed in the schema."""
    records = prepare_ingestion_records(
        "only-doc",
        "doc.pdf",
        [
            {"chunk_number": 1, "original_text": "one"},
            {"chunk_number": 2, "original_text": "two"},
        ],
        workflow_id="wf-live",
    )
    for record in records:
        record.pop("workflow_id", None)
    return _FakeIndex(records, fields={"doc_id", "chunk_num", "instance"})


def test_bulk_purge_degrades_on_an_index_without_a_workflow_id_field(monkeypatch):
    """Without the capability probe this 400s, and the fail-closed purge 502s."""
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
    assert result.get("deleted") is True, f"purge 400'd on a workflow_id-less index: {result}"
    assert [r["chunk_num"] for r in index.records] == [2]
    assert all("workflow_id" not in f for f in index.searches)


def test_index_has_workflow_id_field_probe(monkeypatch):
    """Mirrors ``_index_has_instance_field``; a probe failure keeps the SCOPED
    filter (fail-closed) rather than silently widening the purge."""
    assert api._index_has_workflow_id_field(_FakeIndex([])) is True
    assert api._index_has_workflow_id_field(_legacy_index()) is False

    class _Unreachable:
        def get_settings(self):
            raise RuntimeError("connection refused")

    assert api._index_has_workflow_id_field(_Unreachable()) is True
