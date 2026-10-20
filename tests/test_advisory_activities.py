"""Tests for the advisory index/promote activities, the PROD target and the workflow exports."""

import asyncio
import copy
from pathlib import Path

import pytest

from pipeline import advisory_index
from tests.test_advisory_chunks import SORGHUM_PAYLOAD
from tests.test_advisory_index import FakeStore


def _seed(db):
    db.upsert_webhook_records("webhook", [copy.deepcopy(SORGHUM_PAYLOAD)])
    with db.get_connection() as conn:
        return conn.execute("SELECT id FROM webhook_records").fetchone()["id"]


def _audit_actions(db):
    with db.get_connection() as conn:
        return [r["action_type"] for r in conn.execute("SELECT action_type FROM audit_logs")]


@pytest.fixture
def qdrant_env(monkeypatch):
    monkeypatch.setenv("VECTOR_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_COLLECTION_NAME", "document-data")
    monkeypatch.delenv("PROD_QDRANT_URL", raising=False)
    monkeypatch.setattr("pipeline.vector_store.embeddings.count_tokens", lambda text: 250)


@pytest.mark.unit
@pytest.mark.db
class TestActivities:
    def test_index_activity_writes_to_the_dev_collection_and_audits(self, db_connection, qdrant_env, monkeypatch):
        from pipeline.activities import index_webhook_advisory

        rid = _seed(db_connection)
        store = FakeStore()
        monkeypatch.setattr("pipeline.vector_store.get_vector_store", lambda: store)
        result = asyncio.run(index_webhook_advisory(rid))
        assert result["status"] == "indexed"
        assert store.calls[0][1] == "document-data"
        assert db_connection.get_unindexed_webhook_record_ids() == []
        assert "advisory_indexed" in _audit_actions(db_connection)

    def test_index_activity_rejects_non_qdrant_backend(self, db_connection, monkeypatch):
        from pipeline.activities import index_webhook_advisory

        monkeypatch.setenv("VECTOR_BACKEND", "marqo")
        with pytest.raises(RuntimeError, match="VECTOR_BACKEND=qdrant"):
            asyncio.run(index_webhook_advisory("any"))

    def test_promote_activity_writes_to_prod_and_marks_promoted(self, db_connection, qdrant_env, monkeypatch):
        from pipeline.activities import promote_webhook_advisory

        rid = _seed(db_connection)
        prod = FakeStore()
        monkeypatch.setattr(advisory_index, "prod_target", lambda: (prod, "prod-collection"))
        result = asyncio.run(promote_webhook_advisory(rid))
        assert result["status"] == "indexed"
        assert prod.calls[0][1] == "prod-collection"
        assert db_connection.advisory_status_counts()["promoted"] == 1
        assert "advisory_promoted" in _audit_actions(db_connection)

    def test_promote_activity_requires_a_prod_target(self, db_connection, qdrant_env, monkeypatch):
        from pipeline.activities import promote_webhook_advisory

        monkeypatch.setattr(advisory_index, "prod_target", lambda: None)
        with pytest.raises(RuntimeError, match="PROD_QDRANT_URL"):
            asyncio.run(promote_webhook_advisory("any"))

    def test_dev_index_of_unpublished_record_also_clears_prod(self, db_connection, qdrant_env, monkeypatch):
        from pipeline.activities import index_webhook_advisory

        payload = copy.deepcopy(SORGHUM_PAYLOAD)
        payload["isPublished"] = 0
        db_connection.upsert_webhook_records("webhook", [payload])
        with db_connection.get_connection() as conn:
            rid = conn.execute("SELECT id FROM webhook_records").fetchone()["id"]
        dev, prod = FakeStore(), FakeStore()
        monkeypatch.setattr("pipeline.vector_store.get_vector_store", lambda: dev)
        monkeypatch.setattr(advisory_index, "prod_target", lambda: (prod, "prod-collection"))
        result = asyncio.run(index_webhook_advisory(rid))
        assert result["status"] == "deleted"
        assert prod.calls == [("delete", "prod-collection", rid)]

    def test_dev_index_survives_a_broken_prod_config(self, db_connection, qdrant_env, monkeypatch):
        from pipeline.activities import index_webhook_advisory

        rid = _seed(db_connection)
        monkeypatch.setattr("pipeline.vector_store.get_vector_store", lambda: FakeStore())

        def broken():
            raise ValueError("QDRANT_API_KEY is required for remote Qdrant")

        monkeypatch.setattr(advisory_index, "prod_target", broken)
        assert asyncio.run(index_webhook_advisory(rid))["status"] == "indexed"


class TestProdTarget:
    def test_none_without_prod_url(self, monkeypatch):
        monkeypatch.delenv("PROD_QDRANT_URL", raising=False)
        assert advisory_index.prod_target() is None

    def test_uses_prod_env(self, monkeypatch):
        monkeypatch.setenv("PROD_QDRANT_URL", "http://prod:6333")
        monkeypatch.setenv("PROD_QDRANT_API_KEY", "k")
        monkeypatch.setenv("PROD_QDRANT_COLLECTION_NAME", "prod-docs")
        seen = {}

        def fake_client(url=None, api_key=None):
            seen.update(url=url, api_key=api_key)
            return object()

        monkeypatch.setattr("pipeline.vector_store.qdrant_store.get_qdrant_client", fake_client)
        target = advisory_index.prod_target()
        assert target is not None and target[1] == "prod-docs"
        assert seen == {"url": "http://prod:6333", "api_key": "k"}


def test_workflows_are_defined_and_registered():
    from pipeline.workflows import AdvisoryIndexWorkflow, AdvisoryPromoteWorkflow

    assert callable(AdvisoryIndexWorkflow.run) and callable(AdvisoryPromoteWorkflow.run)
    source = (Path(__file__).resolve().parent.parent / "pipeline" / "worker.py").read_text()
    for name in (
        "AdvisoryIndexWorkflow",
        "AdvisoryPromoteWorkflow",
        "index_webhook_advisory",
        "promote_webhook_advisory",
    ):
        assert source.count(name) >= 2, name  # imported and registered
