"""Opt-in real Qdrant + ephemeral Temporal test. Loopback targets only.

CHUNK_IMPORT_INTEGRATION_URL=http://127.0.0.1:<private-container-port> pytest ...
Creates/removes a unique collection, never adopts an existing collection. Uses
deterministic 1024-dimensional embeddings; this checks wiring, not E5 quality.
Object storage and token validation are test doubles.
"""

import asyncio
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse
import uuid

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from pipeline import embedding, import_state
from pipeline.auth import deps
from pipeline.services import workflow_runtime
from pipeline.temporal import client as temporal_client
from pipeline.temporal.document_tasks import ingest_document_from_db
from pipeline.temporal.document_workflows import ReingestionWorkflow
from pipeline.vector_store import get_vector_store
from tests.helpers import member
from tests.test_chunk_import import Objects
from tests.test_qdrant_local_contract import _Embedder


class _IntegrationEmbedder(_Embedder):
    dense_dim = 1024

    def embed_passages(self, texts):
        batch = super().embed_passages(texts)
        return embedding.EmbeddedBatch(
            dense=[vector + [0.0] * (self.dense_dim - len(vector)) for vector in batch.dense],
            sparse=batch.sparse,
        )

    embed_queries = embed_passages


@pytest.mark.skipif(not os.environ.get("CHUNK_IMPORT_INTEGRATION_URL"), reason="requires an isolated loopback backend")
def test_api_to_real_temporal_and_backend(test_client, db_connection, monkeypatch):
    url = os.environ["CHUNK_IMPORT_INTEGRATION_URL"]
    if urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Integration test refuses a non-loopback backend")
    logical = "import_" + uuid.uuid4().hex[:12]
    registry_name = "t-localtest-" + logical
    physical = "qdrant-final-" + logical
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_URL", url)
    monkeypatch.setenv("QDRANT_INDEX_MAP", f"{registry_name}={physical}")
    monkeypatch.setattr(embedding, "_EMBEDDER", _IntegrationEmbedder())
    monkeypatch.setenv("CHUNK_IMPORT_DEPLOYMENT", "dev")
    monkeypatch.setenv("CHUNK_IMPORT_DEV_TARGETS", json.dumps({"localtest": logical}))
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setattr(workflow_runtime, "_instance_search_attr_supported", False)
    monkeypatch.setattr(deps, "decode_and_validate_token", lambda token, cfg: member("localtest", "content_curator"))
    from pipeline.rate_limit import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr("pipeline.storage.minio.get_client", lambda: Objects())
    db_connection.create_tenant_row("localtest")
    db_connection.create_index_row("localtest", logical, registry_name, is_default=False)
    store = get_vector_store()
    store.create_index(physical, {})
    test_client.headers["Authorization"] = "Bearer local-curator"
    try:
        async def run():
            cache = Path(tempfile.gettempdir()) / "docs-pipeline-186-temporal"
            cache.mkdir(parents=True, exist_ok=True)
            environment = await WorkflowEnvironment.start_local(download_dest_dir=str(cache))
            try:
                monkeypatch.setattr(temporal_client, "_client", environment.client)
                async with Worker(environment.client, task_queue=temporal_client.TASK_QUEUE,
                                  workflows=[ReingestionWorkflow], activities=[ingest_document_from_db]):
                    rows = [{"chunk_number": 3, "text": "Cattle require clean drinking water.", "is_reference": True,
                             "domain_tags": ["species:cattle"]},
                            {"chunk_number": 9, "text": "This deliberately excluded note must not be indexed.", "is_excluded": True}]
                    response = await asyncio.to_thread(test_client.post, "/documents/import-chunks",
                        data={"instance":"localtest", "filename":"Local integration notes", "document_id":uuid.uuid4().hex},
                        files={"chunks":("chunks.json", json.dumps(rows), "application/json")})
                    assert response.status_code == 202, response.text
                    workflow_id = response.json()["workflow_id"]
                    assert import_state.publication(workflow_id)["backend"] == "qdrant"
                    assert import_state.publication(workflow_id)["physical_index"] == physical
                    job = db_connection.get_latest_document_job(workflow_id)
                    result = await asyncio.wait_for(
                        environment.client.get_workflow_handle(job["temporal_workflow_id"]).result(), timeout=180)
                    assert result["verified"] and result["records_ingested"] == 1
                    assert db_connection.get_document(workflow_id)["stage"] == "completed"
                    assert db_connection.get_latest_document_job(workflow_id)["status"] == "completed"
                    records = store.search(physical, q="", filter_string=f"workflow_id:{workflow_id}", limit=10)["hits"]
                    assert len(records) == 1 and records[0]["chunk_num"] == 3
                    assert records[0]["is_reference"] is True and records[0]["domain_tags"] == "|species:cattle|"
                    assert db_connection.get_pages(workflow_id) == []
                    for method in ("TENSOR", "LEXICAL", "HYBRID"):
                        hits = store.search(physical, q="clean drinking water", search_method=method,
                            filter_string=f"workflow_id:{workflow_id}", limit=10)["hits"]
                        assert len(hits) == 1 and hits[0]["chunk_num"] == 3
                    # An explicit retry must repair a real external deletion.
                    document = db_connection.get_document(workflow_id)
                    assert not store.delete_document(document["document_id"], physical, workflow_id=workflow_id).get("error")
                    assert store.get_stats(physical)["numberOfDocuments"] == 0
                    retried = await asyncio.to_thread(test_client.post, f"/documents/{workflow_id}/retry-import")
                    assert retried.status_code == 202, retried.text
                    new_job = db_connection.get_latest_document_job(workflow_id)
                    assert new_job["id"] != job["id"]
                    repaired = await asyncio.wait_for(
                        environment.client.get_workflow_handle(new_job["temporal_workflow_id"]).result(), timeout=180)
                    assert repaired["verified"] and store.get_stats(physical)["numberOfDocuments"] == 1
            finally:
                await environment.shutdown()
        asyncio.run(run())
    finally:
        store.delete_index(physical)
