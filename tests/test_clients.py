"""
Guards for the lazy client seam (pipeline/clients.py).

The contract these lock, in order of how easy it is to break by accident:

1. `clients.get_temporal_client()` MUST return an injected `clients._temporal_client`
   verbatim and MUST NOT attempt a connection. Test fixtures use that explicit
   cache seam; if the accessor ever stops reading it, those fixtures silently
   stop injecting. `test_injected_temporal_client_*`
   is the tripwire — deleting the `if temporal_client is None:` cache-read in
   `clients.get_temporal_client` must make it fail.
2. Reporting endpoints degrade (None) on a Temporal outage; working routes still
   raise loudly.
3. `clients._minio_client` has the identical injection contract.
"""

import asyncio

import pytest

from pipeline import clients, db
from pipeline.app import app


def _run(coro):
    return asyncio.run(coro)


class _Boom(Exception):
    """Distinctive failure so we can tell a real connect attempt apart."""


@pytest.fixture(autouse=True)
def _no_leaked_clients(monkeypatch):
    """Every test here starts from 'nothing cached, nothing injected'."""
    monkeypatch.setattr(clients, "_temporal_client", None)
    monkeypatch.setattr(clients, "_minio_client", None)
    clients.reset()
    yield
    clients.reset()


# ---------------------------------------------------------------------------
# 1. Injection must short-circuit the connect (the monkeypatch compatibility
#    contract this whole refactor rests on).
# ---------------------------------------------------------------------------

class TestInjectedClientsWin:

    @pytest.mark.unit
    def test_injected_temporal_client_is_returned_verbatim(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(clients, "_temporal_client", sentinel)

        assert _run(clients.get_temporal_client()) is sentinel

    @pytest.mark.unit
    def test_injected_temporal_client_prevents_any_connect(self, monkeypatch):
        """The accessor must READ the module global, not just fall back to it.

        Spying on the underlying Temporal connector ensures the real accessor
        reads its injected cache before attempting network I/O.
        """
        calls = []

        async def _spy(target):
            _ = target
            calls.append(1)
            raise _Boom("connect attempted despite an injected client")

        monkeypatch.setattr(clients.Client, "connect", staticmethod(_spy))

        sentinel = object()
        monkeypatch.setattr(clients, "_temporal_client", sentinel)

        assert _run(clients.get_temporal_client()) is sentinel
        assert calls == [], "clients.get_temporal_client() ignored the injected client"

    @pytest.mark.unit
    def test_injected_minio_client_prevents_any_connect(self, monkeypatch):
        calls = []

        def _spy(*args, **kwargs):
            _ = args, kwargs
            calls.append(1)
            raise _Boom("connect attempted despite an injected client")

        monkeypatch.setattr(clients, "Minio", _spy)

        sentinel = object()
        monkeypatch.setattr(clients, "_minio_client", sentinel)

        assert clients.get_minio_client() is sentinel
        assert calls == [], "clients.get_minio_client() ignored the injected client"

    @pytest.mark.unit
    def test_uninjected_accessor_connects_once_and_caches(self, monkeypatch):
        """The other half: with nothing injected, connect lazily and cache."""
        made = []
        connected = object()

        async def _fake_connect(target):
            _ = target
            made.append(1)
            return connected

        monkeypatch.setattr(clients.Client, "connect", staticmethod(_fake_connect))

        assert _run(clients.get_temporal_client()) is connected
        assert clients._temporal_client is connected  # cached on the module global
        assert _run(clients.get_temporal_client()) is connected
        assert made == [1], "lazy client was re-connected instead of cached"


# ---------------------------------------------------------------------------
# 2. Outage behaviour: reporting degrades, real work fails loudly.
# ---------------------------------------------------------------------------

class TestTemporalOutage:

    @pytest.mark.unit
    def test_temporal_client_or_none_swallows_connect_failure(self, monkeypatch):
        async def _fail(target):
            _ = target
            raise _Boom("no temporal")

        monkeypatch.setattr(clients.Client, "connect", staticmethod(_fail))

        assert _run(clients.get_temporal_client_or_none()) is None

    @pytest.mark.unit
    def test_get_temporal_client_raises_when_connect_fails(self, monkeypatch):
        async def _fail(target):
            _ = target
            raise _Boom("no temporal")

        monkeypatch.setattr(clients.Client, "connect", staticmethod(_fail))

        with pytest.raises(RuntimeError, match="no temporal"):
            _run(clients.get_temporal_client())
        assert clients._temporal_client is None, "a failed connect must not be cached"

    @pytest.mark.unit
    def test_clients_get_temporal_client_reports_host_on_failure(self, monkeypatch):
        """clients.py wraps the raw transport error with the host it tried."""
        async def _refuse(target):
            raise OSError("connection refused")

        monkeypatch.setattr(clients.Client, "connect", staticmethod(_refuse))
        monkeypatch.setenv("TEMPORAL_HOST", "temporal.invalid:7233")

        with pytest.raises(RuntimeError, match="temporal.invalid:7233"):
            _run(clients.get_temporal_client())

    @pytest.mark.api
    def test_health_reports_down_temporal_without_raising(self, monkeypatch):
        from fastapi.testclient import TestClient

        async def _fail(target):
            _ = target
            raise _Boom("no temporal")

        monkeypatch.setattr(clients.Client, "connect", staticmethod(_fail))

        db.init_db()
        with TestClient(app) as client:
            response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "temporal_connected": False}

    @pytest.mark.api
    def test_route_needing_temporal_still_fails_loudly(self, monkeypatch, db_connection):
        """A route that genuinely needs Temporal must error, not silently no-op."""
        from fastapi.testclient import TestClient

        async def _fail(target):
            _ = target
            raise _Boom("temporal-is-down-marker")

        monkeypatch.setattr(clients.Client, "connect", staticmethod(_fail))

        db_connection.upsert_document(
            workflow_id="wf-outage",
            document_id="doc-outage",
            filename="f.pdf",
            filepath="/tmp/f.pdf",
            stage="registered",
        )

        with TestClient(app) as client:
            response = client.get("/documents/wf-outage/error-details")

        assert response.status_code >= 400, "Temporal outage was silently swallowed"
        assert "temporal-is-down-marker" in str(response.json())
