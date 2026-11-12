"""Exercise the real Qdrant SDK in memory, without services or model downloads."""

from collections import Counter
import hashlib
import warnings

import pytest

qdrant_client = pytest.importorskip("qdrant_client")

from pipeline.embedding import EmbeddedBatch, SparseVector
from pipeline.vector_store_qdrant import QdrantStore, record_id_to_point_id


class _Embedder:
    dense_dim = 4

    def embed_sparse_plain(self, texts):
        vectors = []
        for text in texts:
            counts = Counter(text.lower().split())
            terms = sorted(counts)
            vectors.append(SparseVector(
                indices=[sum(map(ord, word)) for word in terms],
                values=[float(counts[word]) for word in terms],
            ))
        return vectors

    def embed_passages(self, texts):
        return EmbeddedBatch(
            dense=[
                [0.0, 1.0, 0.0, 0.0] if "beta" in text else [1.0, 0.0, 0.0, 0.0]
                for text in texts
            ],
            sparse=self.embed_sparse_plain(texts),
        )

    embed_queries = embed_passages


@pytest.fixture
def store():
    client = qdrant_client.QdrantClient(":memory:")
    store = QdrantStore(client=client, embedder=_Embedder())
    # Local Qdrant supports filtering but does not implement payload indexes.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Payload indexes have no effect.*")
        store.create_index("vet", {})
    yield store
    client.close()


def _record(number, *, workflow="wf-a", instance="tenant-a"):
    return {
        "_id": hashlib.md5(f"{workflow}:{number}".encode()).hexdigest(),
        "doc_id": "shared-content",
        "workflow_id": workflow,
        "instance": instance,
        "chunk_num": number,
        "text": "alpha veterinary",
        "description": "",
        "text_for_embedding": "passage: alpha veterinary",
        "is_reference": False,
        "query_enabled": True,
    }


@pytest.mark.parametrize("method", ["TENSOR", "LEXICAL", "HYBRID"])
def test_search_respects_tenant_and_visibility_filters(store, method):
    records = [_record(1), _record(2), _record(1, workflow="wf-b", instance="tenant-b")]
    records[1]["query_enabled"] = False
    store.add_documents("vet", records)
    hits = store.search(
        "vet", q="alpha", search_method=method,
        filter_string="instance:(tenant-a) AND query_enabled:true", limit=10,
    )["hits"]
    assert [hit["_id"] for hit in hits] == [_record(1)["_id"]]


def test_metadata_patch_keeps_text_vectors_and_scope(store):
    record = _record(1)
    store.add_documents("vet", [record])
    point_id = record_id_to_point_id(record["_id"])
    before = store.client().retrieve("vet", [point_id], with_vectors=True)[0]
    store.update_documents("vet", [{"_id": record["_id"], "query_enabled": False}])
    after = store.client().retrieve("vet", [point_id], with_vectors=True)[0]
    assert after.vector == before.vector
    assert after.payload == {**before.payload, "query_enabled": False}


def test_ingest_stores_overlapping_sentence_vectors_per_original_chunk(store):
    record = _record(1)
    record["text"] = "First sentence. Second sentence. Third sentence. Fourth sentence."
    record["text_for_embedding"] = f"passage: {record['text']}"
    store.add_documents("vet", [record])

    point = store.client().retrieve(
        "vet", [record_id_to_point_id(record["_id"])], with_vectors=True
    )[0]

    assert len(point.vector["dense"]) == 2
    assert point.payload["chunk_num"] == 1


def test_lexical_search_does_not_call_dense_embedding_service(store, monkeypatch):
    store.add_documents("vet", [_record(1)])

    def unavailable(*_args):
        raise AssertionError("lexical search must not compute dense embeddings")

    monkeypatch.setattr(store.embedder, "embed_queries", unavailable)
    assert store.search("vet", q="alpha", search_method="LEXICAL")["hits"]


def test_lexical_search_includes_description_like_marqo(store):
    record = _record(1)
    record["description"] = "parvo_unique clinical summary"
    store.add_documents("vet", [record])

    hits = store.search("vet", q="parvo_unique", search_method="LEXICAL")["hits"]

    assert [hit["_id"] for hit in hits] == [record["_id"]]


def test_hybrid_alpha_weights_semantic_and_bm25_candidates():
    from qdrant_client.http.models import ScoredPoint
    from pipeline.vector_store_qdrant import _fuse_hybrid_hits

    dense = [ScoredPoint(id="semantic", version=1, score=0.8, payload={"text": "semantic"})]
    sparse = [ScoredPoint(id="keyword", version=1, score=8.0, payload={"text": "keyword"})]

    semantic_first = _fuse_hybrid_hits(
        dense, sparse, alpha=1.0, ranking_method="rrf", rrf_k=60, offset=0, limit=2
    )
    keyword_first = _fuse_hybrid_hits(
        dense, sparse, alpha=0.0, ranking_method="rrf", rrf_k=60, offset=0, limit=2
    )

    assert semantic_first[0].id == "semantic"
    assert keyword_first[0].id == "keyword"


def test_hybrid_normalize_linear_uses_alpha():
    from qdrant_client.http.models import ScoredPoint
    from pipeline.vector_store_qdrant import _fuse_hybrid_hits

    dense = [ScoredPoint(id="semantic", version=1, score=0.9, payload={})]
    sparse = [ScoredPoint(id="keyword", version=1, score=9.0, payload={})]
    hits = _fuse_hybrid_hits(
        dense, sparse, alpha=0.25, ranking_method="normalize_linear", rrf_k=60,
        offset=0, limit=2,
    )

    assert hits[0].id == "keyword"


def test_purge_is_workflow_scoped_and_exhaustive(store):
    store.add_documents("vet", [_record(i) for i in range(1105)], batch_size=100)
    store.add_documents("vet", [_record(1, workflow="wf-b")])
    result = store.delete_document("shared-content", "vet", workflow_id="wf-a")
    assert not result.get("error"), result
    assert result["deleted"] == 1105
    assert store.get_stats("vet")["numberOfDocuments"] == 1
    assert store.get_document("vet", _record(1, workflow="wf-b")["_id"])["workflow_id"] == "wf-b"


def test_text_patch_embeds_new_text_and_preserves_scope(store):
    record = _record(1)
    store.add_documents("vet", [record])
    point_id = record_id_to_point_id(record["_id"])
    before = store.client().retrieve("vet", [point_id], with_vectors=True)[0]
    store.update_documents("vet", [{"_id": record["_id"], "text": "beta veterinary"}])
    after = store.client().retrieve("vet", [point_id], with_vectors=True)[0]
    assert after.vector["dense"] != before.vector["dense"]
    assert after.payload["text_for_embedding"] == "passage: beta veterinary"
    assert after.payload["instance"] == record["instance"]
    assert after.payload["workflow_id"] == record["workflow_id"]


def test_text_patch_of_missing_record_does_not_create_orphan(store):
    from pipeline.vector_store import VectorStoreError

    with pytest.raises(VectorStoreError, match="not found"):
        store.update_documents("vet", [{"_id": _record(1)["_id"], "text": "beta"}])
    assert store.get_stats("vet")["numberOfDocuments"] == 0


def test_delete_chunk_keeps_siblings_and_other_workflows(store):
    store.add_documents("vet", [_record(1), _record(2), _record(1, workflow="wf-b")])
    result = store.delete_chunk("shared-content", 1, "vet", workflow_id="wf-a")
    assert result["deleted"] is True, result
    assert store.get_stats("vet")["numberOfDocuments"] == 2


def test_visibility_patch_does_not_claim_missing_points_updated(store):
    store.add_documents("vet", [_record(1)])
    existing = _record(1)["_id"]
    result = store.set_query_enabled("vet", [existing, "missing"], False)
    assert result["updated"] == 1
    assert result["succeeded_ids"] == [existing]
    assert [failure["_id"] for failure in result["failed"]] == ["missing"]
    assert store.get_document("vet", existing)["query_enabled"] is False


def test_visibility_patch_reports_partial_backend_failure(store, monkeypatch):
    store.add_documents("vet", [_record(1), _record(2)])
    original = store.client().set_payload

    def fail_second(**kwargs):
        if kwargs["points"] == [record_id_to_point_id(_record(2)["_id"])]:
            raise RuntimeError("injected write failure")
        return original(**kwargs)

    monkeypatch.setattr(store.client(), "set_payload", fail_second)
    first, second = _record(1)["_id"], _record(2)["_id"]
    result = store.set_query_enabled("vet", [first, second], False)
    assert result["updated"] == 1
    assert result["succeeded_ids"] == [first]
    assert [failure["_id"] for failure in result["failed"]] == [second]
    assert store.get_document("vet", second)["query_enabled"] is True
