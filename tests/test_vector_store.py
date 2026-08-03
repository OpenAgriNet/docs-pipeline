"""Vector-store adapter contract.

The routes depend on three behaviours that used to be re-derived at each call
site, so they are pinned here:

* a purge distinguishes "nothing to remove" from "the backend failed", because
  only the second may abort a purge-before-flip sequence;
* ``has_field`` never raises, because the tenant-scoping filter treats "cannot
  confirm" as "does not have it" and fails closed on that answer;
* the adapter stays free of FastAPI, so HTTP policy cannot leak into it.
"""

from __future__ import annotations

import sys

import pytest

from pipeline.vector_store import (
    MarqoStore,
    VectorStoreError,
    get_vector_store,
    index_missing_error,
)


class _FakeIndex:
    def __init__(self, hits=None, settings=None, raises=None):
        self._hits = hits if hits is not None else []
        self._settings = settings or {"allFields": []}
        self._raises = raises
        self.deleted_ids: list[list[str]] = []
        self.searches: list[dict] = []

    def search(self, **kwargs):
        self.searches.append(kwargs)
        if self._raises:
            raise self._raises
        return {"hits": self._hits}

    def delete_documents(self, ids):
        self.deleted_ids.append(list(ids))

    def get_settings(self):
        if self._raises:
            raise self._raises
        return self._settings

    def get_stats(self):
        if self._raises:
            raise self._raises
        return {"numberOfDocuments": len(self._hits)}

    def get_document(self, doc_id):
        if self._raises:
            raise self._raises
        return {"_id": doc_id}


def _install(monkeypatch, index: _FakeIndex) -> _FakeIndex:
    class _Client:
        def __init__(self, url):
            self.url = url

        def index(self, _name):
            return index

    monkeypatch.setitem(sys.modules, "marqo", type("m", (), {"Client": _Client}))
    return index


# --------------------------------------------------------------------- purges


def test_delete_document_reports_count_and_removes_every_hit(monkeypatch):
    index = _install(monkeypatch, _FakeIndex(hits=[{"_id": "a"}, {"_id": "b"}]))

    result = MarqoStore().delete_document("doc-1", "an-index")

    assert result == {"deleted": 2, "doc_id": "doc-1"}
    assert index.deleted_ids == [["a", "b"]]


def test_delete_document_with_no_hits_is_not_an_error(monkeypatch):
    index = _install(monkeypatch, _FakeIndex(hits=[]))

    result = MarqoStore().delete_document("doc-1", "an-index")

    assert result["deleted"] == 0
    assert "error" not in result
    assert index.deleted_ids == []


def test_delete_chunk_returns_the_removed_backend_id(monkeypatch):
    index = _install(monkeypatch, _FakeIndex(hits=[{"_id": "doc-1:3"}]))

    result = MarqoStore().delete_chunk("doc-1", 3, "an-index")

    assert result == {"deleted": True, "chunk_id": "doc-1:3"}
    assert index.deleted_ids == [["doc-1:3"]]


def test_delete_chunk_missing_chunk_is_not_an_error(monkeypatch):
    _install(monkeypatch, _FakeIndex(hits=[]))

    result = MarqoStore().delete_chunk("doc-1", 3, "an-index")

    assert result == {"deleted": False, "reason": "not_found"}


@pytest.mark.parametrize("method", ["delete_document", "delete_chunk"])
def test_backend_failure_surfaces_as_error_not_a_benign_miss(monkeypatch, method):
    """A real failure must be distinguishable, or a purge-before-flip route would
    flip the DB after failing to remove anything from search."""
    _install(monkeypatch, _FakeIndex(raises=RuntimeError("connection refused")))
    store = MarqoStore()

    args = ("doc-1", "an-index") if method == "delete_document" else ("doc-1", 3, "an-index")
    result = getattr(store, method)(*args)

    assert result["error"] == "connection refused"
    assert result.get("reason") != "index_missing"


def test_purges_never_raise_even_when_the_backend_is_gone(monkeypatch):
    _install(monkeypatch, _FakeIndex(raises=RuntimeError("Index not found")))
    store = MarqoStore()

    assert store.delete_document("doc-1", "gone")["reason"] == "index_missing"
    assert store.delete_chunk("doc-1", 1, "gone")["reason"] == "index_missing"


@pytest.mark.parametrize("method", ["delete_document", "delete_chunk"])
def test_purges_never_request_id_as_a_retrievable_attribute(monkeypatch, method):
    """Regression: a structured index 400s the whole query when asked for `_id`.

    It comes back on every hit anyway, so a purge asks for a real field and reads
    `_id` off the result. Requesting it here took down every read and purge
    against the legacy amul-veterinary-index (#55).
    """
    index = _install(monkeypatch, _FakeIndex(hits=[{"_id": "a"}]))
    store = MarqoStore()

    args = ("doc-1", "an-index") if method == "delete_document" else ("doc-1", 3, "an-index")
    getattr(store, method)(*args)

    assert index.searches, "the purge should have searched for ids to delete"
    for call in index.searches:
        assert "_id" not in call["attributes_to_retrieve"]


def test_purges_still_delete_by_id_from_the_hits(monkeypatch):
    """The other half of the fix: `_id` is not *requested*, but is still *used*."""
    index = _install(monkeypatch, _FakeIndex(hits=[{"_id": "doc-1:3", "doc_id": "doc-1"}]))

    assert MarqoStore().delete_chunk("doc-1", 3, "an-index") == {
        "deleted": True,
        "chunk_id": "doc-1:3",
    }
    assert index.deleted_ids == [["doc-1:3"]]


def test_index_missing_error_matches_marqo_phrasings():
    assert index_missing_error("Index not found")
    assert index_missing_error(RuntimeError("index abc does not exist"))
    assert not index_missing_error("connection refused")


# ------------------------------------------------------------- introspection


def test_field_names_unwraps_the_allfields_shape(monkeypatch):
    _install(
        monkeypatch,
        _FakeIndex(settings={"allFields": [{"name": "text"}, {"name": "instance"}, {}]}),
    )

    assert MarqoStore().field_names("an-index") == {"text", "instance"}


def test_has_field_is_false_when_the_index_cannot_be_reached(monkeypatch):
    """Fail-closed: `_marqo_instance_filter` reads this as "no tenant scoping
    available" and matches nothing rather than reading the whole corpus."""
    _install(monkeypatch, _FakeIndex(raises=RuntimeError("connection refused")))

    assert MarqoStore().has_field("an-index", "instance") is False


def test_reads_raise_a_typed_error(monkeypatch):
    _install(monkeypatch, _FakeIndex(raises=RuntimeError("boom")))
    store = MarqoStore()

    for call in (
        lambda: store.search("an-index", q="x"),
        lambda: store.get_settings("an-index"),
        lambda: store.get_stats("an-index"),
        lambda: store.get_document("an-index", "some-id"),
    ):
        with pytest.raises(VectorStoreError):
            call()


# ------------------------------------------------------------------ wiring


def test_url_follows_the_environment_at_call_time(monkeypatch):
    store = MarqoStore()
    monkeypatch.setenv("MARQO_URL", "http://first:8882")
    assert store.url == "http://first:8882"
    monkeypatch.setenv("MARQO_URL", "http://second:8882")
    assert store.url == "http://second:8882"


def test_get_vector_store_returns_the_marqo_implementation():
    assert isinstance(get_vector_store(), MarqoStore)


def test_adapter_does_not_depend_on_the_web_framework():
    """Layering guard: HTTP status policy belongs to the routes, not the store.

    If this fails, someone has reached for FastAPI inside the adapter — the
    coupling this module exists to prevent.
    """
    import ast

    import pipeline.vector_store as module

    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "fastapi" not in imported
    # The auth and registry layers are the store's callers, never its dependencies.
    assert not {"db", "auth"} & imported
