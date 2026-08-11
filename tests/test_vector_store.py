"""Vector-store adapter contract.

The routes depend on behaviours that used to be re-derived at every call site.
Now that they have one home they get pinned here, at the seam, rather than only
through a route:

* a purge is scoped to ONE document and pages until the filter is empty — there
  is no ceiling on how many records a document may have (#73);
* a purge reports "nothing to remove" and "the backend failed" as different
  outcomes, because only the second may abort a purge-before-flip sequence;
* a purge never asks for ``_id`` in ``attributes_to_retrieve`` — a structured
  legacy index 400s the whole query if it does (prod hotfix #55);
* the two capability probes disagree on their error branch on purpose;
* the adapter stays free of FastAPI, so HTTP policy cannot leak into it.

All Marqo interaction is an in-memory fake. Nothing here touches a live index.
"""

from __future__ import annotations

import sys

import pytest

from pipeline.vector_store import (
    MarqoStore,
    VectorStoreError,
    get_vector_store,
    index_has_instance_field,
    index_has_workflow_id_field,
    index_missing_error,
    marqo_doc_scope_filter,
)

LEGACY_INDEX = "legacy-vet-index"
TENANT_INDEX = "t-tenant-a-vet"


class _FakeIndex:
    """In-memory index mimicking the structured-index filter contract.

    ``fields`` is the set of names the index advertises as filterable. Filtering
    on anything else raises the way Marqo does (HTTP 400 / "no filterable
    field"), and asking for ``_id`` back raises the way a structured index does.
    """

    def __init__(self, records: list[dict], fields: set[str] | None = None):
        self.records = records
        self.fields = (
            fields if fields is not None else {"doc_id", "workflow_id", "chunk_num"}
        )
        self.searches: list[str] = []
        self.deleted: list[list[str]] = []

    def get_settings(self):
        return {"allFields": [{"name": n, "type": "text"} for n in sorted(self.fields)]}

    def get_stats(self):
        return {"numberOfDocuments": len(self.records)}

    def get_document(self, _id):
        for record in self.records:
            if record["_id"] == _id:
                return record
        raise RuntimeError("document not found")

    def search(self, q="", filter_string="", limit=10, attributes_to_retrieve=None):
        self.searches.append(filter_string)
        if attributes_to_retrieve and "_id" in attributes_to_retrieve:
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
            r
            for r in self.records
            if all(str(r.get(f, "")) == v for f, v in wanted.items())
        ]
        keep = list(attributes_to_retrieve or []) + ["_id"]
        return {"hits": [{k: h[k] for k in keep if k in h} for h in hits[:limit]]}

    def delete_documents(self, ids):
        self.deleted.append(list(ids))
        self.records[:] = [r for r in self.records if r["_id"] not in set(ids)]


def _install(monkeypatch, index: _FakeIndex, *, exists: bool = False) -> _FakeIndex:
    """Patch the ``marqo`` module so the store's lazy import picks up the fake.

    ``exists`` drives ``get_index``, which is how Marqo signals index existence:
    it raises when there is no such index.
    """

    class _Client:
        def __init__(self, url=None, **_kwargs):
            self.url = url

        def index(self, _name):
            return index

        def get_index(self, name):
            if exists:
                return {"indexName": name}
            raise RuntimeError(f"index {name} not found")

    monkeypatch.setitem(sys.modules, "marqo", type("m", (), {"Client": _Client}))
    return index


def _records(document_id: str, workflow_id: str | None, count: int, start: int = 1):
    out = []
    for n in range(start, start + count):
        record = {
            "_id": f"{workflow_id or 'none'}-{n}",
            "doc_id": document_id,
            "chunk_num": n,
        }
        if workflow_id:
            record["workflow_id"] = workflow_id
        out.append(record)
    return out


# =============================================================================
# Scoping — the purge stays inside one document
# =============================================================================


def test_scope_filter_is_unscoped_only_without_a_workflow_id():
    assert marqo_doc_scope_filter("d1") == "doc_id:d1"
    assert marqo_doc_scope_filter("d1", "wf-1") == "doc_id:d1 AND workflow_id:wf-1"


def test_delete_document_leaves_a_co_resident_document_alone(monkeypatch):
    """Two rows may share a ``document_id``; purging one must not sweep the
    other's records (#73)."""
    index = _install(
        monkeypatch,
        _FakeIndex(_records("shared", "wf-a", 3) + _records("shared", "wf-b", 2)),
    )

    result = MarqoStore().delete_document("shared", TENANT_INDEX, workflow_id="wf-b")

    assert result["deleted"] == 2
    assert sorted(r["workflow_id"] for r in index.records) == ["wf-a"] * 3


def test_delete_chunk_is_scoped_to_one_document(monkeypatch):
    """Chunk 3 belongs to ``wf-a`` only; purging ``wf-b``'s chunk 3 must miss."""
    index = _install(
        monkeypatch,
        _FakeIndex(_records("shared", "wf-a", 3) + _records("shared", "wf-b", 2)),
    )

    result = MarqoStore().delete_chunk("shared", 3, TENANT_INDEX, workflow_id="wf-b")

    assert result["deleted"] is False
    assert len(index.records) == 5


# =============================================================================
# No chunk cap — the purge pages until the filter is empty
# =============================================================================


def test_delete_document_has_no_ceiling_on_records_per_document(monkeypatch):
    """A single search page is 1,000 records. A 1,400-chunk document must still
    come back fully purged, not report a truncated success (#73)."""
    index = _install(monkeypatch, _FakeIndex(_records("big", "wf-big", 1400)))

    result = MarqoStore().delete_document("big", TENANT_INDEX, workflow_id="wf-big")

    assert result["deleted"] == 1400
    assert index.records == []
    assert len(index.deleted) > 1, "expected the purge to page, not one flat search"


def test_delete_document_pages_on_the_degraded_path_too(monkeypatch):
    """The cap was a defect independent of scoping — it bit on an index that
    cannot answer a ``workflow_id`` filter as well."""
    index = _install(
        monkeypatch,
        _FakeIndex(_records("big", None, 1200), fields={"doc_id", "chunk_num"}),
    )

    result = MarqoStore().delete_document("big", LEGACY_INDEX, workflow_id="wf-big")

    assert result["deleted"] == 1200
    assert index.records == []


# =============================================================================
# Fail-closed refusals
# =============================================================================


def test_unattributable_records_are_refused_not_guessed(monkeypatch):
    """Records carrying no ``workflow_id`` cannot be attributed, and the index
    state is byte-identical to "already purged, these are someone else's". The
    purge refuses rather than widening an irreversible delete."""
    index = _install(
        monkeypatch,
        _FakeIndex(_records("shared", None, 2) + _records("shared", "wf-b", 1)),
    )

    result = MarqoStore().delete_document("shared", TENANT_INDEX, workflow_id="wf-x")

    assert result["deleted"] == 0
    assert "refusing to purge" in result["error"]
    assert len(index.records) == 3


def test_a_delete_that_does_not_take_is_never_reported_as_success(monkeypatch):
    """Reporting still-searchable records as deleted is #73's exact symptom."""

    class _Stubborn(_FakeIndex):
        def delete_documents(self, ids):
            return {"status": "ok"}

    index = _install(monkeypatch, _Stubborn(_records("stuck", "wf-s", 2)))

    result = MarqoStore().delete_document("stuck", TENANT_INDEX, workflow_id="wf-s")

    assert result["deleted"] == 0
    assert "still searchable" in result["error"]
    assert len(index.records) == 2, "the report must match reality"


def test_already_purged_is_benign_when_nothing_shares_the_doc_id(monkeypatch):
    _install(monkeypatch, _FakeIndex(_records("solo", "wf-solo", 1)))
    store = MarqoStore()

    assert store.delete_document("solo", TENANT_INDEX, workflow_id="wf-solo")["deleted"] == 1
    again = store.delete_document("solo", TENANT_INDEX, workflow_id="wf-solo")
    assert again["deleted"] == 0 and "error" not in again


def test_missing_index_is_benign_on_both_purge_paths(monkeypatch):
    class _Missing(_FakeIndex):
        def search(self, **_kwargs):
            raise RuntimeError("index not found: no such index")

    _install(monkeypatch, _Missing([]))
    store = MarqoStore()

    bulk = store.delete_document("d", TENANT_INDEX, workflow_id="wf")
    assert bulk == {"deleted": 0, "doc_id": "d", "reason": "index_missing"}

    one = store.delete_chunk("d", 1, TENANT_INDEX, workflow_id="wf")
    assert one == {"deleted": False, "reason": "index_missing"}


def test_index_missing_error_only_matches_a_missing_index():
    assert index_missing_error("Index not found") is True
    assert index_missing_error(RuntimeError("index does not exist")) is True
    assert index_missing_error(RuntimeError("connection refused")) is False


# =============================================================================
# #55 — a structured index rejects `_id` as a retrievable attribute
# =============================================================================


@pytest.mark.parametrize("purge", ["document", "chunk"])
def test_a_purge_never_asks_the_index_for_id(monkeypatch, purge):
    """The fake 400s on ``_id`` exactly as the live legacy index did. A purge
    that survives it is one that asked for a real field instead."""
    asked: list[list[str] | None] = []

    class _Recording(_FakeIndex):
        def search(self, q="", filter_string="", limit=10, attributes_to_retrieve=None):
            asked.append(attributes_to_retrieve)
            return super().search(
                q=q,
                filter_string=filter_string,
                limit=limit,
                attributes_to_retrieve=attributes_to_retrieve,
            )

    _install(monkeypatch, _Recording(_records("d", "wf", 2)))
    store = MarqoStore()

    if purge == "document":
        result = store.delete_document("d", LEGACY_INDEX, workflow_id="wf")
        assert result["deleted"] == 2
    else:
        result = store.delete_chunk("d", 1, LEGACY_INDEX, workflow_id="wf")
        assert result["deleted"] is True

    assert asked, "no search was issued"
    assert all(attrs == ["doc_id"] for attrs in asked), asked


# =============================================================================
# Capability probes — the asymmetry is deliberate
# =============================================================================


def test_the_two_probes_disagree_on_their_error_branch():
    """A ``workflow_id`` probe failure keeps the NARROW filter; an ``instance``
    probe failure drops to "absent". Guessing "absent" for ``workflow_id`` would
    silently widen every purge on a transient hiccup."""

    class _Unreachable:
        def get_settings(self):
            raise RuntimeError("connection refused")

    assert index_has_workflow_id_field(_Unreachable()) is True
    assert index_has_instance_field(_Unreachable()) is False


def test_probes_read_the_live_field_list():
    stamped = _FakeIndex([], fields={"doc_id", "workflow_id", "instance"})
    legacy = _FakeIndex([], fields={"doc_id", "chunk_num"})

    assert index_has_workflow_id_field(stamped) is True
    assert index_has_instance_field(stamped) is True
    assert index_has_workflow_id_field(legacy) is False
    assert index_has_instance_field(legacy) is False


def test_purge_degrades_instead_of_400ing_on_an_index_without_workflow_id(monkeypatch):
    index = _install(
        monkeypatch,
        _FakeIndex(_records("only", None, 2), fields={"doc_id", "chunk_num"}),
    )

    result = MarqoStore().delete_document("only", LEGACY_INDEX, workflow_id="wf-live")

    assert "error" not in result, result
    assert result["deleted"] == 2
    assert all("workflow_id" not in f for f in index.searches)


# =============================================================================
# Reads, error translation, and the layering rules
# =============================================================================


def test_reads_wrap_backend_failures_in_vector_store_error(monkeypatch):
    class _Broken(_FakeIndex):
        def get_settings(self):
            raise RuntimeError("connection refused")

        def get_stats(self):
            raise RuntimeError("connection refused")

        def search(self, **_kwargs):
            raise RuntimeError("connection refused")

    _install(monkeypatch, _Broken([]))
    store = MarqoStore()

    for call in (
        lambda: store.get_settings(TENANT_INDEX),
        lambda: store.get_stats(TENANT_INDEX),
        lambda: store.field_names(TENANT_INDEX),
        lambda: store.search(TENANT_INDEX, q="x"),
        lambda: store.get_document(TENANT_INDEX, "missing"),
    ):
        with pytest.raises(VectorStoreError):
            call()


def test_field_names_unwraps_the_all_fields_shape(monkeypatch):
    _install(monkeypatch, _FakeIndex([], fields={"doc_id", "domain_tags"}))
    assert MarqoStore().field_names(TENANT_INDEX) == {"doc_id", "domain_tags"}


def test_index_exists_never_raises(monkeypatch):
    _install(monkeypatch, _FakeIndex([]))
    assert MarqoStore().index_exists("nope") is False


def test_get_vector_store_honours_an_injected_client_factory():
    """Client construction is injectable, and that injection point is the seam
    the rest of the suite swaps. Nothing may bypass it and dial Marqo directly."""
    calls = {"n": 0}
    index = _FakeIndex(_records("d", "wf", 1))

    class _Client:
        def index(self, _name):
            return index

    def _factory():
        calls["n"] += 1
        return _Client()

    store = get_vector_store(client_factory=_factory)
    assert store.delete_document("d", TENANT_INDEX, workflow_id="wf")["deleted"] == 1
    assert calls["n"] > 0


# =============================================================================
# The write path — what it reports, and what it refuses to decide
# =============================================================================


def test_describe_index_reports_drift_without_acting_on_it(monkeypatch):
    """``describe_index`` measures; provisioning, warning and refusing are the
    ingest activity's decisions, and it must be able to tell them apart."""
    _install(monkeypatch, _FakeIndex([], fields={"doc_id", "text"}), exists=True)
    report = MarqoStore().describe_index(TENANT_INDEX)

    assert report.exists is True
    assert report.field_names == {"doc_id", "text"}
    assert report.has_passage_tensor is False, "the fake declares no tensorFields"
    assert "workflow_id" in report.missing_core


def test_describe_index_never_flattens_a_failed_read_into_an_empty_report(monkeypatch):
    """The bug this guards: a transient ``get_settings`` blip reported as "the
    index declares no fields" reads as confirmed drift, and the ingest activity
    would stop re-raising for Temporal to retry."""

    class _Blip(_FakeIndex):
        def get_settings(self):
            raise RuntimeError("transient marqo blip")

    _install(monkeypatch, _Blip([]), exists=True)
    with pytest.raises(RuntimeError, match="transient"):
        MarqoStore().describe_index(TENANT_INDEX)


def test_describe_index_reports_a_missing_index_as_absent_not_empty(monkeypatch):
    _install(monkeypatch, _FakeIndex([]))  # the fake's get_index always raises
    assert MarqoStore().describe_index("nope").exists is False


def test_project_records_does_not_mutate_the_caller_list():
    """The same list is archived to object storage before ingest. Rewriting it in
    place made the exported payload and the ingested one disagree."""
    from pipeline.vector_store import project_records

    records = [{"_id": "1", "text": "x", "bogus": 1, "section": "S"}]
    original = [dict(r) for r in records]

    projected = project_records(records, {"text", "section"}, {"text"})

    assert records == original, "the caller's records were rewritten under it"
    assert projected == [{"_id": "1", "text": "x"}]


def test_add_documents_batches_and_unwraps_marqos_partial_failure_shape(monkeypatch):
    """Marqo reports per-item failures inside a 200. The adapter unwraps that
    shape; whether it aborts the ingest is the caller's call, via ``on_batch``."""
    seen: list[list[dict]] = []

    class _Writer(_FakeIndex):
        def add_documents(self, batch):
            seen.append(batch)
            if len(seen) == 2:
                return {
                    "errors": True,
                    "items": [
                        {"_id": "c", "status": 400, "error": "bad field", "code": "x"},
                        {"_id": "d", "status": 200},
                    ],
                }
            return {"errors": False, "items": []}

    _install(monkeypatch, _Writer([]), exists=True)
    hooked: list[list[dict]] = []
    result = MarqoStore().add_documents(
        TENANT_INDEX,
        [{"_id": c} for c in "abcd"],
        batch_size=2,
        on_batch=lambda errors, _raw: hooked.append(errors),
    )

    assert [len(b) for b in seen] == [2, 2], "expected two batches of two"
    assert hooked == [[], [{"_id": "c", "status": 400, "error": "bad field", "message": None, "code": "x"}]]
    assert result.batches == 2
    assert [e["_id"] for e in result.errors] == ["c"]


def test_the_adapter_does_not_import_fastapi():
    """Layering: failures raise VectorStoreError and the caller picks the status.
    An `HTTPException` in here would put HTTP policy behind the seam."""
    import ast

    import pipeline.vector_store as vector_store

    tree = ast.parse(open(vector_store.__file__, encoding="utf-8").read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "fastapi" not in imported, imported
    assert not any(
        isinstance(node, ast.Name) and node.id == "HTTPException"
        for node in ast.walk(tree)
    )


def test_vector_store_is_the_only_place_a_marqo_client_is_built():
    """One definition of client construction, package-wide.

    The previous version of this test counted ``marqo.Client(`` in
    ``pipeline/api.py`` alone and asserted ``== 1``. That scan was wrong twice
    over: it never saw the second inline client in ``pipeline/activities.py``,
    and it would have failed *for the wrong reason* once the refactor dropped
    api.py's count to 0. Scan the whole package and name the one file allowed to
    build a client instead.

    Ops ``scripts/`` must also stay on :func:`get_vector_store` — a direct
    ``import marqo`` / ``marqo.Client(`` there re-opens the seam at the package
    boundary (#90 item 5).
    """
    import pathlib
    import re

    import pipeline

    root = pathlib.Path(pipeline.__file__).parent
    builders = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "marqo.Client(" in path.read_text(encoding="utf-8")
    )
    assert builders == ["vector_store.py"], (
        f"Marqo clients are built outside the adapter: {builders}"
    )

    scripts_root = root.parent / "scripts"
    script_hits = sorted(
        path.relative_to(scripts_root).as_posix()
        for path in scripts_root.rglob("*.py")
        if re.search(
            r"(?m)^(import marqo\b|from marqo\b)|marqo\.Client\(",
            path.read_text(encoding="utf-8"),
        )
    )
    assert script_hits == [], (
        f"Ops scripts import/build Marqo outside get_vector_store: {script_hits}"
    )


def test_get_vector_store_url_override_keeps_client_inside_adapter(monkeypatch):
    """Ops scripts may pass url= without constructing marqo.Client themselves."""
    calls: list[str] = []

    class _Client:
        def __init__(self, url=None, **_kwargs):
            calls.append(url)

    monkeypatch.setitem(sys.modules, "marqo", type("m", (), {"Client": _Client}))
    store = get_vector_store(url="http://marqo.test:9999")
    assert store.url == "http://marqo.test:9999"
    store.client()
    assert calls == ["http://marqo.test:9999"]

    with pytest.raises(ValueError):
        get_vector_store(client_factory=lambda: None, url="http://x")
