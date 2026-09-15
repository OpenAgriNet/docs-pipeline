"""Unit tests for the Qdrant vector-store adapter (#151)."""

from __future__ import annotations

import os

import pytest

from pipeline.embedding import (
    SparseVector,
    ensure_passage_prefix,
    ensure_query_prefix,
    l2_normalize,
    reset_embedder,
)
from pipeline.vector_store import default_physical_index, get_vector_store, vector_store_backend
from pipeline.vector_store_qdrant import parse_filter_string, record_id_to_point_id


def test_e5_prefixes():
    assert ensure_passage_prefix("hello").startswith("passage:")
    assert ensure_passage_prefix("passage: already") == "passage: already"
    assert ensure_query_prefix("hello").startswith("query:")
    assert ensure_query_prefix("query: already") == "query: already"


def test_l2_normalize_unit_length():
    vector = l2_normalize([3.0, 4.0])
    assert vector == pytest.approx([0.6, 0.8])


def test_record_id_to_point_id_is_stable_uuid():
    a = record_id_to_point_id("a" * 32)
    b = record_id_to_point_id("a" * 32)
    c = record_id_to_point_id("b" * 32)
    assert a == b
    assert a != c
    assert len(a) == 36


def test_parse_filter_is_reference_false():
    filt = parse_filter_string("is_reference:false")
    assert filt is not None
    assert filt.must
    cond = filt.must[0]
    assert cond.key == "is_reference"
    assert cond.match.value is False


def test_parse_filter_and_instance():
    filt = parse_filter_string("is_reference:false AND instance:(amul)")
    assert filt is not None
    assert len(filt.must) == 2
    keys = {cond.key for cond in filt.must}
    assert keys == {"is_reference", "instance"}


def test_parse_filter_domain_tag():
    filt = parse_filter_string("domain_tags:(|species:cattle|)")
    assert filt is not None
    cond = filt.must[0]
    assert cond.key == "domain_tags"


def test_vector_store_backend_defaults_to_marqo(monkeypatch):
    monkeypatch.delenv("VECTOR_STORE_BACKEND", raising=False)
    assert vector_store_backend() == "marqo"


def test_default_physical_index_uses_qdrant_name(monkeypatch):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("MARQO_INDEX_NAME", "rebuild")
    monkeypatch.setenv("QDRANT_INDEX_NAME", "rebuild-qdrant")
    assert default_physical_index() == "rebuild-qdrant"
    monkeypatch.delenv("QDRANT_INDEX_NAME", raising=False)
    assert default_physical_index() == "rebuild"


def test_default_physical_index_marqo_ignores_qdrant_name(monkeypatch):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "marqo")
    monkeypatch.setenv("MARQO_INDEX_NAME", "rebuild")
    monkeypatch.setenv("QDRANT_INDEX_NAME", "rebuild-qdrant")
    assert default_physical_index() == "rebuild"


def test_get_vector_store_qdrant_branch(monkeypatch):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.test:6333")

    class _FakeEmbedder:
        dense_dim = 8

        def embed_passages(self, texts):
            raise NotImplementedError

        def embed_queries(self, texts):
            raise NotImplementedError

        def embed_sparse_plain(self, texts):
            return [SparseVector(indices=[1], values=[1.0]) for _ in texts]

    reset_embedder(_FakeEmbedder())
    try:
        store = get_vector_store()
        assert store.url == "http://qdrant.test:6333"
        assert store.__class__.__name__ == "QdrantStore"
    finally:
        reset_embedder(None)
        monkeypatch.delenv("VECTOR_STORE_BACKEND", raising=False)
