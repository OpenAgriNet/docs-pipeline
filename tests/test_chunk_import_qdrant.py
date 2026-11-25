"""Prepared imports through the real Qdrant SDK, with deterministic embeddings.

SQLite and ASGI are real; Temporal and object storage use the shared test doubles.
No external service, model download or deployed collection is used.
"""

import hashlib
import json
import warnings

import pytest

qdrant_client = pytest.importorskip("qdrant_client")

from pipeline import embedding, import_state
from pipeline.vector_store import get_vector_store
from pipeline.vector_store_qdrant import QdrantStore
from tests.test_chunk_import_api import TARGET, imports, publish, upload
from tests.test_chunk_import import pdf
from tests.test_qdrant_local_contract import _Embedder


@pytest.fixture
def qd(imports, monkeypatch):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid")
    monkeypatch.setenv("QDRANT_INDEX_NAME", "final-test")
    monkeypatch.setenv("QDRANT_INDEX_MAP", f"{TARGET}=final-test")
    monkeypatch.setenv("QDRANT_INDEX_SUFFIX", "")
    monkeypatch.setattr(embedding, "_EMBEDDER", _Embedder())
    client = qdrant_client.QdrantClient(":memory:")
    monkeypatch.setattr(QdrantStore, "client", lambda self: client)
    store = get_vector_store()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Payload indexes have no effect.*")
        store.create_index("final-test", {})
    imports.qdrant = store
    imports.collection = "final-test"
    imports.client = client
    yield imports
    assert not imports.store.constructions, "Qdrant imports must never construct Marqo"
    client.close()


def records(qd, workflow):
    return qd.qdrant.search(qd.collection, q="", filter_string=f"workflow_id:{workflow}", limit=100)["hits"]


def test_qdrant_import_pins_mapped_collection_and_preserves_chunk_payload(qd):
    response = upload(qd, [
        {"chunk_number": 3, "text": "  Gujarati દૂધ guidance\n", "is_reference": False,
         "domain_tags": ["species:cattle"], "section_title": "Feeding"},
        {"chunk_number": 7, "text": "References", "is_reference": True},
        {"chunk_number": 9, "text": "Excluded", "is_excluded": True},
    ])
    assert response.status_code == 202, response.text
    wf = response.json()["workflow_id"]
    pin = import_state.publication(wf)
    assert pin["physical_index"] == "final-test" and pin["backend"] == "qdrant"
    assert qd.temporal.start_workflow.call_args.kwargs["args"][5] == "final-test"
    assert not qd.client.collection_exists(TARGET)
    assert publish(qd, wf) == {"records_ingested": 2, "verified": True}
    hits = sorted(records(qd, wf), key=lambda row: row["chunk_num"])
    assert [row["chunk_num"] for row in hits] == [3, 7]
    # Publication applies the pipeline's normal ingestion text cleanup.
    assert hits[0]["text"] == "Gujarati દૂધ guidance"
    assert hits[0]["domain_tags"] == "|species:cattle|" and hits[0]["section"] == "Feeding"
    assert [row["is_reference"] for row in hits] == [False, True]
    assert qd.db.get_document(wf)["query_enabled"]
    assert publish(qd, wf)["verified"]


@pytest.mark.parametrize("change", ["mapping", "endpoint", "backend"])
def test_qdrant_destination_drift_rejects_worker_and_retry(qd, monkeypatch, change):
    wf = upload(qd).json()["workflow_id"]
    if change == "mapping":
        monkeypatch.setenv("QDRANT_INDEX_MAP", f"{TARGET}=wrong-test")
    elif change == "endpoint":
        monkeypatch.setenv("QDRANT_URL", "http://wrong.invalid")
    else:
        # Even the same URL and collection cannot change backend kind.
        monkeypatch.setenv("MARQO_URL", "http://qdrant.invalid")
        monkeypatch.setenv("VECTOR_STORE_BACKEND", "marqo")
        with qd.db.get_connection() as conn, conn:
            conn.execute("UPDATE prepared_imports SET physical_index = ? WHERE workflow_id = ?", (TARGET, wf))
    with pytest.raises(RuntimeError, match="destination changed"):
        publish(qd, wf)
    assert qd.api.post(f"/documents/{wf}/retry-import").status_code == 409
    assert not qd.db.get_document(wf)["query_enabled"]
    assert qd.client.count("final-test").count == 0


@pytest.mark.parametrize("failure", ["batch", "missing", "corrupt", "cleanup"])
def test_qdrant_failed_publication_is_scoped_and_retry_repairs_it(qd, monkeypatch, failure):
    sentinel = {"_id": hashlib.md5(b"other").hexdigest(), "workflow_id": "other", "doc_id": "other",
                "instance": "a", "text": "Existing content", "text_for_embedding": "passage: Existing content"}
    qd.qdrant.add_documents(qd.collection, [sentinel])
    wf = upload(qd, [{"chunk_number": n, "text": f"Row {n}"} for n in range(1, 23)]).json()["workflow_id"]
    with monkeypatch.context() as patch:
        if failure == "batch":
            original = qd.client.upsert
            calls = 0
            def partial(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("second batch failed")
                return original(*args, **kwargs)
            patch.setattr(qd.client, "upsert", partial)
        else:
            read = QdrantStore.get_documents
            def invalid(self, *args, **kwargs):
                found = read(self, *args, **kwargs)
                if failure == "corrupt":
                    found[0]["is_reference"] = True
                    return found
                return found[:-1]
            patch.setattr(QdrantStore, "get_documents", invalid)
            if failure == "cleanup":
                def broken(*args, **kwargs):
                    raise RuntimeError("cleanup unavailable")
                patch.setattr(qd.client, "delete", broken)
        with pytest.raises(RuntimeError, match="batch|readback|cleanup"):
            publish(qd, wf)
    status = import_state.publication(wf)
    assert status["status"] == ("cleanup_failed" if failure == "cleanup" else "failed")
    assert bool(status["cleanup_required"]) == (failure == "cleanup")
    assert not qd.db.get_document(wf)["query_enabled"]
    assert len(records(qd, wf)) == (22 if failure == "cleanup" else 0)
    assert qd.qdrant.get_document(qd.collection, sentinel["_id"])["text"] == "Existing content"
    assert qd.api.post(f"/documents/{wf}/retry-import").status_code == 202
    assert publish(qd, wf) == {"records_ingested": 22, "verified": True}
    assert qd.client.count(qd.collection).count == 23


def test_qdrant_batch_readback_is_unordered_bounded_and_detects_missing_ids(qd, monkeypatch):
    wf = upload(qd, [{"chunk_number": n, "text": f"Row {n}"} for n in range(1, 202)]).json()["workflow_id"]
    read = qd.client.retrieve
    sizes = []
    def unordered(*args, **kwargs):
        sizes.append(len(kwargs["ids"]))
        assert kwargs["with_vectors"] is False and kwargs["with_payload"] is True
        return list(reversed(read(*args, **kwargs)))
    monkeypatch.setattr(qd.client, "retrieve", unordered)
    assert publish(qd, wf)["records_ingested"] == 201
    assert sizes == [100, 100, 1]
    assert qd.qdrant.get_documents(qd.collection, []) == []
    assert qd.qdrant.get_documents(qd.collection, [hashlib.md5(b"missing").hexdigest()]) == []


def test_legacy_marqo_receipt_migration_remains_marqo(qd):
    # Exercise the additive migration against the actual previous table shape.
    with qd.db.get_connection() as conn, conn:
        conn.execute("DROP TABLE prepared_imports")
        conn.execute("""CREATE TABLE prepared_imports (
            workflow_id TEXT PRIMARY KEY, logical_index TEXT NOT NULL, physical_index TEXT NOT NULL,
            backend_url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'prepared', attempt_id TEXT,
            job_id INTEGER, manifest_hash TEXT, error_message TEXT, updated_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO prepared_imports (workflow_id, logical_index, physical_index, backend_url, updated_at) VALUES ('old', 'x', 'x', 'http://old.invalid', 'then')")
    qd.db.init_db()
    qd.db.init_db()  # migration is repeatable
    assert import_state.publication("old")["backend"] == "marqo"


def test_qdrant_rejects_incompatible_collection_before_storage_or_job(qd):
    from qdrant_client.http import models
    qd.client.delete_collection(qd.collection)
    qd.client.create_collection(qd.collection, vectors_config={
        "dense": models.VectorParams(size=8, distance=models.Distance.DOT),
    })
    response = upload(qd)
    assert response.status_code == 503
    assert not qd.objects.uploads and not qd.temporal.start_workflow.called
    assert not qd.db.list_documents(instances=["a"])


def test_qdrant_import_keeps_pdf_page_bounds_and_preview(qd):
    content = pdf(pages=3)
    response = qd.api.post("/documents/import-chunks",
        data={"instance": "a", "document_id": "pdf-import", "filename": "Treatment.pdf"},
        files={"chunks": ("chunks.json", json.dumps([
            {"chunk_number": 7, "text": "Treatment details", "page_start": 2, "page_end": 3},
        ]), "application/json"), "source_pdf": ("Treatment.pdf", content, "application/pdf")})
    assert response.status_code == 202, response.text
    wf = response.json()["workflow_id"]
    assert publish(qd, wf)["verified"]
    record = records(qd, wf)[0]
    assert (record["chunk_num"], record["page_start"], record["page_end"]) == (7, 2, 3)
    assert qd.api.get(f"/documents/{wf}/pdf").content == content


def test_qdrant_verifies_full_10000_chunk_import_without_search_pagination(qd, monkeypatch):
    rows = [{"chunk_number": n, "text": f"Guidance {n}"} for n in range(1, 10_001)]
    response = upload(qd, rows)
    assert response.status_code == 202, response.text
    wf = response.json()["workflow_id"]
    original = qd.client.retrieve
    batches = []
    def bounded(*args, **kwargs):
        batches.append(len(kwargs["ids"]))
        return original(*args, **kwargs)
    monkeypatch.setattr(qd.client, "retrieve", bounded)
    assert publish(qd, wf)["records_ingested"] == 10_000
    assert batches == [100] * 100
    assert qd.client.count(qd.collection).count == 10_000
