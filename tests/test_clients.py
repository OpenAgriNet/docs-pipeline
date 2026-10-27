"""The lazy Temporal client accessor connects once and caches; on a Temporal outage
reporting endpoints degrade (None) while working routes still raise loudly."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from pipeline import db
from pipeline.app import app
from pipeline.storage import minio as minio_storage
from pipeline.temporal import client as temporal_client


class _Boom(Exception):
    """Distinctive, so a real connect attempt is told apart."""


@pytest.fixture(autouse=True)
def _no_leaked_clients(monkeypatch):
    """Every test starts with nothing cached and nothing injected."""
    monkeypatch.setattr(temporal_client, "_client", None)
    monkeypatch.setattr(minio_storage, "_client", None)
    temporal_client.reset()
    minio_storage.reset()
    yield
    temporal_client.reset()
    minio_storage.reset()


def _connect_raises(monkeypatch, error):
    async def _connect(_target):
        raise error

    monkeypatch.setattr(temporal_client.Client, "connect", staticmethod(_connect))


def test_uninjected_accessor_connects_once_and_caches(monkeypatch):
    made, connected = [], object()

    async def _connect(_target):
        made.append(1)
        return connected

    monkeypatch.setattr(temporal_client.Client, "connect", staticmethod(_connect))
    assert asyncio.run(temporal_client.get_client()) is connected
    assert temporal_client._client is connected, "not cached on the module global"
    assert asyncio.run(temporal_client.get_client()) is connected
    assert made == [1], "lazy client was re-connected instead of cached"


def test_temporal_client_or_none_swallows_connect_failure(monkeypatch):
    _connect_raises(monkeypatch, _Boom("no temporal"))
    assert asyncio.run(temporal_client.get_client_or_none()) is None


def test_get_temporal_client_raises_when_connect_fails(monkeypatch):
    _connect_raises(monkeypatch, _Boom("no temporal"))
    with pytest.raises(RuntimeError, match="no temporal"):
        asyncio.run(temporal_client.get_client())
    assert temporal_client._client is None, "a failed connect must not be cached"


def test_clients_get_temporal_client_reports_host_on_failure(monkeypatch):
    """The Temporal adapter wraps transport errors with the target host."""
    _connect_raises(monkeypatch, OSError("connection refused"))
    monkeypatch.setenv("TEMPORAL_HOST", "temporal.invalid:7233")
    with pytest.raises(RuntimeError, match="temporal.invalid:7233"):
        asyncio.run(temporal_client.get_client())


def test_health_reports_down_temporal_without_raising(monkeypatch):
    _connect_raises(monkeypatch, _Boom("no temporal"))
    db.init_db()
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "temporal_connected": False}


def test_route_needing_temporal_still_fails_loudly(monkeypatch, db_connection):
    """A route that genuinely needs Temporal must error, not silently no-op."""
    _connect_raises(monkeypatch, _Boom("temporal-is-down-marker"))
    db_connection.upsert_document(
        workflow_id="wf-outage", document_id="doc-outage", filename="f.pdf", filepath="/tmp/f.pdf", stage="registered"
    )
    with TestClient(app) as client:
        response = client.get("/documents/wf-outage/error-details")
    assert response.status_code >= 400, "Temporal outage was silently swallowed"
    assert "temporal-is-down-marker" in str(response.json())
