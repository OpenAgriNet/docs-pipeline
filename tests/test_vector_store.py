"""Vector-store adapter contract, pinned at the seam rather than through a route.

* a purge is scoped to ONE document and pages until the filter is empty (#73);
* a purge reports "nothing to remove" and "the backend failed" differently,
  because only the second may abort a purge-before-flip sequence;
* a purge never asks for ``_id`` back (a structured index 400s, prod hotfix #55);
* the two capability probes disagree on their error branch on purpose.
"""

from __future__ import annotations

import pytest

from pipeline import vector_store as vector_store_mod
from pipeline.vector_store import (
    MarqoRetrievalTruncatedError,
    MarqoStore,
    VectorStoreError,
    get_vector_store,
    index_has_instance_field,
    index_has_workflow_id_field,
    index_missing_error,
    marqo_doc_scope_filter,
    project_records,
    search_all_hits,
)
from tests.helpers import FakeIndex, FakeMarqo

INDEX = "t-tenant-a-vet"
#: An index created before ``workflow_id`` joined the schema.
LEGACY_FIELDS = {"doc_id", "chunk_num"}


def _records(document_id: str, workflow_id: str | None, count: int):
    out = []
    for n in range(1, count + 1):
        record = {"_id": f"{workflow_id or 'none'}-{n}", "doc_id": document_id, "chunk_num": n}
        if workflow_id:
            record["workflow_id"] = workflow_id
        out.append(record)
    return out


def _co_resident(fake_marqo):
    """Two documents sharing one ``document_id``: wf-a (3 chunks), wf-b (2)."""
    return fake_marqo.add_index(INDEX, _records("shared", "wf-a", 3) + _records("shared", "wf-b", 2))


# --- scoping: the purge stays inside one document -------------------------------


def test_scope_filter_is_unscoped_only_without_a_workflow_id():
    assert marqo_doc_scope_filter("d1") == "doc_id:d1"
    assert marqo_doc_scope_filter("d1", "wf-1") == "doc_id:d1 AND workflow_id:wf-1"


def test_delete_document_leaves_a_co_resident_document_alone(fake_marqo):
    index = _co_resident(fake_marqo)
    assert MarqoStore().delete_document("shared", INDEX, workflow_id="wf-b")["deleted"] == 2
    assert sorted(r["workflow_id"] for r in index.records) == ["wf-a"] * 3


def test_delete_chunk_is_scoped_to_one_document(fake_marqo):
    """Chunk 3 belongs to ``wf-a`` only; purging ``wf-b``'s chunk 3 must miss."""
    index = _co_resident(fake_marqo)
    assert MarqoStore().delete_chunk("shared", 3, INDEX, workflow_id="wf-b")["deleted"] is False
    assert len(index.records) == 5


# --- no chunk cap: the purge pages until the filter is empty --------------------


@pytest.mark.parametrize(
    "fields, stamp", [(None, "wf-big"), (LEGACY_FIELDS, None)], ids=["scoped", "index-without-workflow-id"]
)
def test_delete_document_has_no_ceiling_on_records_per_document(fake_marqo, fields, stamp):
    """A search page is 1,000 records; a 1,400-chunk document must still come back
    fully purged, not report a truncated success (#73), on either purge path."""
    index = fake_marqo.add_index(INDEX, _records("big", stamp, 1400), fields=fields)
    assert MarqoStore().delete_document("big", INDEX, workflow_id="wf-big")["deleted"] == 1400
    assert index.records == []
    assert len(index.deleted) > 1, "expected the purge to page, not one flat search"


# --- status reads: page until empty, fail closed at Marqo's offset cap ----------


def test_search_all_hits_pages_past_a_single_limit(fake_marqo):
    """GET status used to issue one limit=1000 search and drop the tail."""
    index = fake_marqo.add_index(INDEX, _records("big", "wf-big", 1400))
    hits = search_all_hits(
        MarqoStore(), INDEX, "doc_id:big AND workflow_id:wf-big", ["doc_id", "chunk_num"], page_size=1000
    )
    assert len(hits) == 1400
    assert len(index.searches) == 2


def test_search_all_hits_raises_at_the_retrievable_offset_cap(fake_marqo, monkeypatch):
    """Offset cannot walk past 10000; a truncated list must not look complete."""
    monkeypatch.setattr(vector_store_mod, "_MARQO_MAX_RETRIEVABLE_OFFSET", 4)
    fake_marqo.add_index(INDEX, _records("cap", "wf-cap", 6))
    with pytest.raises(MarqoRetrievalTruncatedError):
        search_all_hits(
            MarqoStore(), INDEX, "doc_id:cap AND workflow_id:wf-cap", ["doc_id", "chunk_num"], page_size=2
        )


# --- fail-closed refusals --------------------------------------------------------


def test_unattributable_records_are_refused_not_guessed(fake_marqo):
    """Records without a ``workflow_id`` look exactly like "already purged, these
    are someone else's", so the purge refuses rather than widen an irreversible delete."""
    index = fake_marqo.add_index(INDEX, _records("shared", None, 2) + _records("shared", "wf-b", 1))
    result = MarqoStore().delete_document("shared", INDEX, workflow_id="wf-x")
    assert result["deleted"] == 0
    assert "refusing to purge" in result["error"]
    assert len(index.records) == 3


def test_a_delete_that_does_not_take_is_never_reported_as_success(fake_marqo, monkeypatch):
    """Reporting still-searchable records as deleted is #73's exact symptom."""
    index = fake_marqo.add_index(INDEX, _records("stuck", "wf-s", 2))
    monkeypatch.setattr(index, "delete_documents", lambda ids: {"status": "ok"})
    result = MarqoStore().delete_document("stuck", INDEX, workflow_id="wf-s")
    assert result["deleted"] == 0
    assert "still searchable" in result["error"]
    assert len(index.records) == 2, "the report must match reality"


def test_already_purged_is_benign_when_nothing_shares_the_doc_id(fake_marqo):
    fake_marqo.add_index(INDEX, _records("solo", "wf-solo", 1))
    store = MarqoStore()
    assert store.delete_document("solo", INDEX, workflow_id="wf-solo")["deleted"] == 1
    again = store.delete_document("solo", INDEX, workflow_id="wf-solo")
    assert again["deleted"] == 0 and "error" not in again


def test_missing_index_is_benign_on_both_purge_paths(fake_marqo):
    store = MarqoStore()  # nothing registered: every call on INDEX raises "index not found"
    assert store.delete_document("d", INDEX, workflow_id="wf") == {
        "deleted": 0, "doc_id": "d", "reason": "index_missing",
    }
    assert store.delete_chunk("d", 1, INDEX, workflow_id="wf") == {"deleted": False, "reason": "index_missing"}


def test_index_missing_error_only_matches_a_missing_index():
    assert index_missing_error("Index not found") is True
    assert index_missing_error(RuntimeError("index does not exist")) is True
    assert index_missing_error(RuntimeError("connection refused")) is False


@pytest.mark.parametrize("purge", ["document", "chunk"])
def test_a_purge_never_asks_the_index_for_id(fake_marqo, purge):
    """The fake 400s on ``_id`` exactly as the live legacy index did (#55)."""
    index = fake_marqo.add_index("legacy-vet-index", _records("d", "wf", 2))
    store = MarqoStore()
    if purge == "document":
        assert store.delete_document("d", "legacy-vet-index", workflow_id="wf")["deleted"] == 2
    else:
        assert store.delete_chunk("d", 1, "legacy-vet-index", workflow_id="wf")["deleted"] is True
    asked = [s["attributes_to_retrieve"] for s in index.searches]
    assert asked, "no search was issued"
    assert all(attrs == ["doc_id"] for attrs in asked), asked


# --- capability probes: the asymmetry is deliberate -----------------------------


def test_the_two_probes_disagree_on_their_error_branch():
    """A ``workflow_id`` probe failure keeps the NARROW filter; an ``instance`` probe
    failure drops to "absent". Guessing "absent" for ``workflow_id`` would silently
    widen every purge on a transient hiccup."""
    unreachable = FakeIndex().fail("get_settings", RuntimeError("connection refused"))
    assert index_has_workflow_id_field(unreachable) is True
    assert index_has_instance_field(unreachable) is False


def test_probes_read_the_live_field_list():
    stamped = FakeIndex(fields={"doc_id", "workflow_id", "instance"})
    legacy = FakeIndex(fields=LEGACY_FIELDS)
    assert index_has_workflow_id_field(stamped) is True
    assert index_has_instance_field(stamped) is True
    assert index_has_workflow_id_field(legacy) is False
    assert index_has_instance_field(legacy) is False


def test_purge_degrades_instead_of_400ing_on_an_index_without_workflow_id(fake_marqo):
    index = fake_marqo.add_index("legacy-vet-index", _records("only", None, 2), fields=LEGACY_FIELDS)
    result = MarqoStore().delete_document("only", "legacy-vet-index", workflow_id="wf-live")
    assert "error" not in result, result
    assert result["deleted"] == 2
    assert all("workflow_id" not in f for f in index.filters)


# --- reads, error translation, client construction ------------------------------


def test_reads_wrap_backend_failures_in_vector_store_error(fake_marqo):
    index = fake_marqo.add_index(INDEX)
    for method in ("get_settings", "get_stats", "search"):
        index.fail(method, RuntimeError("connection refused"))
    store = MarqoStore()
    for call in (
        lambda: store.get_settings(INDEX),
        lambda: store.get_stats(INDEX),
        lambda: store.field_names(INDEX),
        lambda: store.search(INDEX, q="x"),
        lambda: store.get_document(INDEX, "missing"),
    ):
        with pytest.raises(VectorStoreError):
            call()


def test_field_names_unwraps_the_all_fields_shape(fake_marqo):
    fake_marqo.add_index(INDEX, fields={"doc_id", "domain_tags"})
    assert MarqoStore().field_names(INDEX) == {"doc_id", "domain_tags"}


def test_a_missing_index_is_reported_absent_never_raised(fake_marqo):
    assert MarqoStore().index_exists("nope") is False
    assert MarqoStore().describe_index("nope").exists is False


def test_get_vector_store_honours_an_injected_client_factory():
    """Client construction is injectable. The fake is NOT installed in
    ``sys.modules`` here, so a store that bypassed the factory would dial a real
    Marqo and the delete would fail."""
    fake = FakeMarqo()
    fake.add_index(INDEX, _records("d", "wf", 1))
    store = get_vector_store(client_factory=fake.connect)
    assert store.delete_document("d", INDEX, workflow_id="wf")["deleted"] == 1
    fake.assert_constructed()


def test_get_vector_store_url_override_keeps_client_inside_adapter(fake_marqo):
    """Ops scripts may pass url= without constructing marqo.Client themselves."""
    store = get_vector_store(url="http://marqo.test:9999")
    assert store.url == "http://marqo.test:9999"
    store.client()
    assert fake_marqo.constructions == ["http://marqo.test:9999"]
    with pytest.raises(ValueError):
        get_vector_store(client_factory=lambda: None, url="http://x")


# --- the write path: what it reports, and what it refuses to decide -------------


def test_describe_index_reports_drift_without_acting_on_it(fake_marqo):
    """``describe_index`` measures; provisioning, warning and refusing are the
    ingest activity's decisions, and it must be able to tell them apart."""
    fake_marqo.add_index(INDEX, fields={"doc_id", "text"}, tensor_fields=())
    report = MarqoStore().describe_index(INDEX)
    assert report.exists is True
    assert report.field_names == {"doc_id", "text"}
    assert report.has_passage_tensor is False
    assert "workflow_id" in report.missing_core


def test_describe_index_never_flattens_a_failed_read_into_an_empty_report(fake_marqo):
    """A transient ``get_settings`` blip reported as "no fields" reads as confirmed
    drift, and the ingest activity would stop re-raising for Temporal to retry."""
    fake_marqo.add_index(INDEX).fail("get_settings", RuntimeError("transient marqo blip"))
    with pytest.raises(RuntimeError, match="transient"):
        MarqoStore().describe_index(INDEX)


def test_project_records_does_not_mutate_the_caller_list():
    """The same list is archived to object storage before ingest; rewriting it in
    place made the exported payload and the ingested one disagree."""
    records = [{"_id": "1", "text": "x", "bogus": 1, "section": "S"}]
    original = [dict(r) for r in records]
    projected = project_records(records, {"text", "section"}, {"text"})
    assert records == original, "the caller's records were rewritten under it"
    assert projected == [{"_id": "1", "text": "x"}]


def test_add_documents_batches_and_unwraps_marqos_partial_failure_shape(fake_marqo):
    """Marqo reports per-item failures inside a 200. The adapter unwraps that shape;
    whether it aborts the ingest is the caller's call, via ``on_batch``."""
    index = fake_marqo.add_index(INDEX)
    index.rejected["c"] = {"status": 400, "error": "bad field", "code": "x"}
    hooked: list[list[dict]] = []
    result = MarqoStore().add_documents(
        INDEX, [{"_id": c} for c in "abcd"], batch_size=2, on_batch=lambda errors, _raw: hooked.append(errors)
    )
    assert [len(b) for b in index.added] == [2, 2], "expected two batches of two"
    assert hooked == [[], [{"_id": "c", "status": 400, "error": "bad field", "message": None, "code": "x"}]]
    assert result.batches == 2
    assert [e["_id"] for e in result.errors] == ["c"]
