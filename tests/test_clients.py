"""
Guards for the lazy client seam (pipeline/clients.py + the api accessors).

The contract these lock, in order of how easy it is to break by accident:

1. `api.get_temporal_client()` MUST return an injected `api.temporal_client`
   verbatim and MUST NOT attempt a connection. ~40 test sites monkeypatch that
   module attribute; if the accessor ever stops reading it, those patches keep
   "passing" while silently testing nothing. `test_injected_temporal_client_*`
   is the tripwire — deleting the `if temporal_client is None:` cache-read in
   `api.get_temporal_client` must make it fail.
2. Reporting endpoints degrade (None) on a Temporal outage; working routes still
   raise loudly.
3. `api.minio_client` has the identical injection contract.
"""

import asyncio

import pytest

from pipeline import api, clients


def _run(coro):
    return asyncio.run(coro)


class _Boom(Exception):
    """Distinctive failure so we can tell a real connect attempt apart."""


@pytest.fixture(autouse=True)
def _no_leaked_clients(monkeypatch):
    """Every test here starts from 'nothing cached, nothing injected'."""
    monkeypatch.setattr(api, "temporal_client", None)
    monkeypatch.setattr(api, "minio_client", None)
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
        monkeypatch.setattr(api, "temporal_client", sentinel)

        assert _run(api.get_temporal_client()) is sentinel

    @pytest.mark.unit
    def test_injected_temporal_client_prevents_any_connect(self, monkeypatch):
        """The accessor must READ the module global, not just fall back to it.

        Spying on `clients.get_temporal_client` is the point: if the accessor
        delegates unconditionally, the spy fires and this fails.
        """
        calls = []

        async def _spy():
            calls.append(1)
            raise _Boom("connect attempted despite an injected client")

        monkeypatch.setattr(clients, "get_temporal_client", _spy)

        sentinel = object()
        monkeypatch.setattr(api, "temporal_client", sentinel)

        assert _run(api.get_temporal_client()) is sentinel
        assert calls == [], "api.get_temporal_client() ignored the injected client"

    @pytest.mark.unit
    def test_injected_minio_client_prevents_any_connect(self, monkeypatch):
        calls = []

        def _spy():
            calls.append(1)
            raise _Boom("connect attempted despite an injected client")

        monkeypatch.setattr(clients, "get_minio_client", _spy)

        sentinel = object()
        monkeypatch.setattr(api, "minio_client", sentinel)

        assert api.get_minio_client() is sentinel
        assert calls == [], "api.get_minio_client() ignored the injected client"

    @pytest.mark.unit
    def test_uninjected_accessor_connects_once_and_caches(self, monkeypatch):
        """The other half: with nothing injected, connect lazily and cache."""
        made = []
        connected = object()

        async def _fake_connect():
            made.append(1)
            return connected

        monkeypatch.setattr(clients, "get_temporal_client", _fake_connect)

        assert _run(api.get_temporal_client()) is connected
        assert api.temporal_client is connected  # cached on the module global
        assert _run(api.get_temporal_client()) is connected
        assert made == [1], "lazy client was re-connected instead of cached"


# ---------------------------------------------------------------------------
# 2. Outage behaviour: reporting degrades, real work fails loudly.
# ---------------------------------------------------------------------------

class TestTemporalOutage:

    @pytest.mark.unit
    def test_temporal_client_or_none_swallows_connect_failure(self, monkeypatch):
        async def _fail():
            raise _Boom("no temporal")

        monkeypatch.setattr(clients, "get_temporal_client", _fail)

        assert _run(api._temporal_client_or_none()) is None

    @pytest.mark.unit
    def test_get_temporal_client_raises_when_connect_fails(self, monkeypatch):
        async def _fail():
            raise _Boom("no temporal")

        monkeypatch.setattr(clients, "get_temporal_client", _fail)

        with pytest.raises(_Boom):
            _run(api.get_temporal_client())
        assert api.temporal_client is None, "a failed connect must not be cached"

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

        async def _fail():
            raise _Boom("no temporal")

        monkeypatch.setattr(clients, "get_temporal_client", _fail)

        api.db.init_db()
        with TestClient(api.app) as client:
            response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok", "temporal_connected": False}

    @pytest.mark.api
    def test_route_needing_temporal_still_fails_loudly(self, monkeypatch, db_connection):
        """A route that genuinely needs Temporal must error, not silently no-op."""
        from fastapi.testclient import TestClient

        async def _fail():
            raise _Boom("temporal-is-down-marker")

        monkeypatch.setattr(clients, "get_temporal_client", _fail)

        db_connection.upsert_document(
            workflow_id="wf-outage",
            document_id="doc-outage",
            filename="f.pdf",
            filepath="/tmp/f.pdf",
            stage="registered",
        )

        with TestClient(api.app) as client:
            response = client.get("/documents/wf-outage/error-details")

        assert response.status_code >= 400, "Temporal outage was silently swallowed"
        assert "temporal-is-down-marker" in str(response.json())
