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
from pipeline.vector_store import (
    default_physical_index,
    get_vector_store,
    resolve_backend_index,
    vector_store_backend,
)
from pipeline.vector_store_qdrant import (
    _DOMAIN_TAG_KEYS_FIELD,
    _payload_from_record,
    parse_filter_string,
    record_id_to_point_id,
)


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
    assert cond.key == _DOMAIN_TAG_KEYS_FIELD
    assert cond.match.value == "species:cattle"


def test_parse_filter_domain_tag_does_not_match_prefix_sibling():
    cattle = parse_filter_string("domain_tags:(|species:cattle|)")
    breed = parse_filter_string("domain_tags:(|species:cattle-breed|)")
    cattle_cond = cattle.must[0]
    breed_cond = breed.must[0]
    assert cattle_cond.key == _DOMAIN_TAG_KEYS_FIELD
    assert breed_cond.key == _DOMAIN_TAG_KEYS_FIELD
    assert cattle_cond.match.value == "species:cattle"
    assert breed_cond.match.value == "species:cattle-breed"
    assert cattle_cond.match.value != breed_cond.match.value
    assert type(cattle_cond.match).__name__ == "MatchValue"
    assert type(breed_cond.match).__name__ == "MatchValue"


def test_payload_stores_exact_domain_tag_keys_not_pipe_string():
    cattle = _payload_from_record({"_id": "a" * 32, "domain_tags": "|species:cattle|"})
    breed = _payload_from_record({"_id": "b" * 32, "domain_tags": "|species:cattle-breed|"})
    assert cattle[_DOMAIN_TAG_KEYS_FIELD] == ["species:cattle"]
    assert breed[_DOMAIN_TAG_KEYS_FIELD] == ["species:cattle-breed"]
    assert cattle["domain_tags"] == "|species:cattle|"
    assert breed["domain_tags"] == "|species:cattle-breed|"


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


def test_update_documents_metadata_only_uses_set_payload(monkeypatch):
    from pipeline.vector_store_qdrant import QdrantStore, record_id_to_point_id

    calls = []

    class _FakeClient:
        def set_payload(self, **kwargs):
            calls.append(kwargs)

        def retrieve(self, **kwargs):
            raise AssertionError("retrieve should not run for metadata-only updates")

    store = QdrantStore(url="http://qdrant.test:6333", client=_FakeClient())
    monkeypatch.setattr(store, "index_exists", lambda _index: True)
    record_id = "a" * 32
    result = store.update_documents(
        "idx",
        [{"_id": record_id, "instance": "amul"}],
    )
    assert result["updated"] == 1
    assert len(calls) == 1
    assert calls[0]["points"] == [record_id_to_point_id(record_id)]
    assert calls[0]["payload"]["instance"] == "amul"
    assert calls[0]["payload"]["record_id"] == record_id


def test_describe_index_rejects_wrong_dense_dim():
    from types import SimpleNamespace

    from pipeline.vector_store_qdrant import QdrantStore

    class _FakeEmbedder:
        dense_dim = 1024

    class _FakeClient:
        def get_collections(self):
            return SimpleNamespace(collections=[SimpleNamespace(name="idx")])

        def get_collection(self, name):
            dense = SimpleNamespace(size=384, distance=SimpleNamespace(value="Cosine"))
            bm25 = SimpleNamespace(modifier=SimpleNamespace(value="Idf"))
            return SimpleNamespace(
                config=SimpleNamespace(
                    params=SimpleNamespace(
                        vectors={"dense": dense},
                        sparse_vectors={"bm25": bm25},
                    )
                ),
                payload_schema={"doc_id": SimpleNamespace()},
                points_count=1,
            )

    store = QdrantStore(
        url="http://qdrant.test:6333",
        client=_FakeClient(),
        embedder=_FakeEmbedder(),
    )
    report = store.describe_index("idx")
    assert report.exists is True
    assert "text_for_embedding" not in report.tensor_fields
    assert report.has_passage_tensor is False
    assert any("size 384" in error for error in report.schema_errors)


def test_describe_index_rejects_dense_only_collection():
    from types import SimpleNamespace

    from pipeline.vector_store_qdrant import QdrantStore

    class _FakeEmbedder:
        dense_dim = 1024

    class _FakeClient:
        def get_collections(self):
            return SimpleNamespace(collections=[SimpleNamespace(name="idx")])

        def get_collection(self, name):
            dense = SimpleNamespace(size=1024, distance=SimpleNamespace(value="Cosine"))
            return SimpleNamespace(
                config=SimpleNamespace(
                    params=SimpleNamespace(
                        vectors={"dense": dense},
                        sparse_vectors={},
                    )
                ),
                payload_schema={"doc_id": SimpleNamespace()},
                points_count=1,
            )

    store = QdrantStore(
        url="http://qdrant.test:6333",
        client=_FakeClient(),
        embedder=_FakeEmbedder(),
    )
    report = store.describe_index("idx")
    assert report.has_passage_tensor is False
    assert report.tensor_fields == set()
    assert any("bm25" in error for error in report.schema_errors)


def test_describe_index_rejects_bm25_without_idf():
    from types import SimpleNamespace

    from pipeline.vector_store_qdrant import QdrantStore

    class _FakeEmbedder:
        dense_dim = 1024

    class _FakeClient:
        def get_collections(self):
            return SimpleNamespace(collections=[SimpleNamespace(name="idx")])

        def get_collection(self, name):
            dense = SimpleNamespace(size=1024, distance=SimpleNamespace(value="Cosine"))
            return SimpleNamespace(
                config=SimpleNamespace(
                    params=SimpleNamespace(
                        vectors={"dense": dense},
                        sparse_vectors={"bm25": SimpleNamespace()},
                    )
                ),
                payload_schema={"doc_id": SimpleNamespace()},
                points_count=1,
            )

    store = QdrantStore(
        url="http://qdrant.test:6333",
        client=_FakeClient(),
        embedder=_FakeEmbedder(),
    )
    report = store.describe_index("idx")
    assert report.has_passage_tensor is False
    assert any("idf" in error.lower() for error in report.schema_errors)


def test_describe_index_accepts_dense_bm25_idf():
    from types import SimpleNamespace

    from pipeline.vector_store_qdrant import QdrantStore

    class _FakeEmbedder:
        dense_dim = 1024

    class _FakeClient:
        def get_collections(self):
            return SimpleNamespace(collections=[SimpleNamespace(name="idx")])

        def get_collection(self, name):
            dense = SimpleNamespace(size=1024, distance=SimpleNamespace(value="Cosine"))
            bm25 = SimpleNamespace(modifier=SimpleNamespace(value="Idf"))
            return SimpleNamespace(
                config=SimpleNamespace(
                    params=SimpleNamespace(
                        vectors={"dense": dense},
                        sparse_vectors={"bm25": bm25},
                    )
                ),
                payload_schema={"doc_id": SimpleNamespace()},
                points_count=1,
            )

    store = QdrantStore(
        url="http://qdrant.test:6333",
        client=_FakeClient(),
        embedder=_FakeEmbedder(),
    )
    report = store.describe_index("idx")
    assert report.has_passage_tensor is True
    assert "text_for_embedding" in report.tensor_fields
    assert report.schema_errors == ()


def test_resolve_backend_index_maps_seeded_default(monkeypatch):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_INDEX_NAME", "shadow-qdrant")
    monkeypatch.setenv("MARQO_INDEX_NAME", "documents-index")
    monkeypatch.delenv("QDRANT_INDEX_MAP", raising=False)
    monkeypatch.delenv("QDRANT_INDEX_SUFFIX", raising=False)
    assert resolve_backend_index("documents-index") == "shadow-qdrant"
    assert resolve_backend_index("documents-index", backend="qdrant") == "shadow-qdrant"
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "marqo")
    assert resolve_backend_index("documents-index") == "documents-index"
    assert resolve_backend_index("documents-index", backend="qdrant") == "shadow-qdrant"


def test_resolve_backend_index_map_and_suffix(monkeypatch):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_INDEX_NAME", "shadow-qdrant")
    monkeypatch.setenv("QDRANT_INDEX_MAP", "t-tenant-a-vet=t-tenant-a-vet-qdrant")
    monkeypatch.delenv("QDRANT_INDEX_SUFFIX", raising=False)
    assert resolve_backend_index("t-tenant-a-vet") == "t-tenant-a-vet-qdrant"
    monkeypatch.delenv("QDRANT_INDEX_MAP", raising=False)
    monkeypatch.setenv("QDRANT_INDEX_SUFFIX", "-qdrant")
    assert resolve_backend_index("t-tenant-a-vet") == "t-tenant-a-vet-qdrant"


def test_audit_script_bm25lite_check_is_evidence_based():
    from pathlib import Path

    source = Path("scripts/audit_qdrant_cutover_readiness.py").read_text(encoding="utf-8")
    assert "or True" not in source
    assert "inspect.getsource" in source
    assert "qdrant_passage_schema" in source
    assert "cutover_index_resolution" in source
