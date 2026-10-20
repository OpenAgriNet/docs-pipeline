"""API tests: /webhook starts advisory workflows; admin backfill / promote / status endpoints."""

import os
from unittest.mock import AsyncMock, patch

import pytest

TEST_KEY = "0123456789abcdef0123456789abcdef"
H = {"x-api-key": TEST_KEY}


def _rec(content_id, published=1, status="Published"):
    return {
        "contentID": content_id,
        "title": f"Advisory {content_id}",
        "status": status,
        "isPublished": published,
        "popList": [{"popID": 1, "status": "Published"}],
    }


@pytest.fixture
def client(db_connection, monkeypatch):
    from fastapi.testclient import TestClient
    from pipeline import api

    monkeypatch.setattr(api.limiter, "enabled", False)  # many posts per test process
    with patch.dict(os.environ, {"WEBHOOK_API_KEY": TEST_KEY, "AUTH_DISABLED": "true"}):
        yield TestClient(api.app)


@pytest.fixture
def temporal(monkeypatch):
    from pipeline import api

    mock = AsyncMock()
    monkeypatch.setattr(api, "temporal_client", mock)
    return mock


def _row_ids(db, *content_ids):
    with db.get_connection() as conn:
        return {
            c: conn.execute(
                "SELECT id FROM webhook_records WHERE external_id = ?", (str(c),)
            ).fetchone()["id"]
            for c in content_ids
        }


def _started(mock):
    return sorted(call.kwargs["id"] for call in mock.start_workflow.await_args_list)


@pytest.mark.api
@pytest.mark.unit
class TestWebhookStartsIndexing:
    def test_one_workflow_per_record_in_the_call(self, client, db_connection, temporal):
        r = client.post("/webhook", json=[_rec(1), _rec(2)], headers=H)
        assert r.status_code == 200
        assert r.json() == {
            "success": True,
            "message": "Webhook processed successfully.",
            "recordCount": 2,
        }
        ids = _row_ids(db_connection, 1, 2)
        assert _started(temporal) == sorted(f"advisory-{i}" for i in ids.values())

    def test_only_records_in_the_call_are_started(self, client, db_connection, temporal):
        db_connection.upsert_webhook_records("webhook", [_rec(1)])  # existing, unindexed
        client.post("/webhook", json=[_rec(2)], headers=H)
        ids = _row_ids(db_connection, 1, 2)
        assert _started(temporal) == [f"advisory-{ids[2]}"]

    def test_existing_never_indexed_row_is_started_when_resent(self, client, db_connection, temporal):
        db_connection.upsert_webhook_records("webhook", [_rec(1)])
        client.post("/webhook", json=[_rec(1)], headers=H)
        ids = _row_ids(db_connection, 1)
        assert _started(temporal) == [f"advisory-{ids[1]}"]

    def test_nothing_started_when_already_indexed(self, client, db_connection, temporal):
        client.post("/webhook", json=[_rec(1)], headers=H)
        rid = _row_ids(db_connection, 1)[1]
        db_connection.mark_webhook_record_indexed(rid, db_connection.get_webhook_record(rid)["payload_hash"])
        temporal.start_workflow.reset_mock()
        r = client.post("/webhook", json=[_rec(1)], headers=H)
        assert r.status_code == 200
        temporal.start_workflow.assert_not_called()

    def test_still_200_when_temporal_fails(self, client, db_connection, temporal):
        temporal.start_workflow.side_effect = RuntimeError("temporal down")
        r = client.post("/webhook", json=[_rec(1)], headers=H)
        assert r.status_code == 200
        assert len(db_connection.get_unindexed_webhook_record_ids()) == 1

    def test_still_200_when_temporal_client_is_none(self, client, db_connection, monkeypatch):
        from pipeline import api

        monkeypatch.setattr(api, "temporal_client", None)
        assert client.post("/webhook", json=[_rec(1)], headers=H).status_code == 200


@pytest.mark.api
@pytest.mark.unit
class TestAdminEndpoints:
    def _seed(self, client, temporal, *records):
        client.post("/webhook", json=list(records), headers=H)
        temporal.start_workflow.reset_mock()

    def test_reindex_queues_all_unindexed_by_default(self, client, db_connection, temporal):
        self._seed(client, temporal, _rec(1), _rec(2))
        r = client.post("/webhook/advisory/reindex", json={})
        assert r.status_code == 200 and r.json() == {"success": True, "queued": 2}
        assert len(_started(temporal)) == 2

    def test_reindex_force_queues_indexed_rows_too(self, client, db_connection, temporal):
        self._seed(client, temporal, _rec(1))
        rid = _row_ids(db_connection, 1)[1]
        db_connection.mark_webhook_record_indexed(rid, db_connection.get_webhook_record(rid)["payload_hash"])
        assert client.post("/webhook/advisory/reindex", json={}).json()["queued"] == 0
        assert client.post("/webhook/advisory/reindex", json={"force": True}).json()["queued"] == 1

    def test_reindex_explicit_ids(self, client, db_connection, temporal):
        self._seed(client, temporal, _rec(1), _rec(2))
        rid = _row_ids(db_connection, 2)[2]
        r = client.post("/webhook/advisory/reindex", json={"record_ids": [rid]})
        assert r.json()["queued"] == 1
        assert _started(temporal) == [f"advisory-{rid}"]

    def test_promote_defaults_to_published_records(self, client, db_connection, temporal):
        self._seed(client, temporal, _rec(1), _rec(2, published=0, status="Draft"))
        r = client.post("/webhook/advisory/promote", json={})
        assert r.json() == {"success": True, "queued": 1}
        published = _row_ids(db_connection, 1)[1]
        assert _started(temporal) == [f"advisory-promote-{published}"]

    def test_promote_explicit_ids(self, client, db_connection, temporal):
        self._seed(client, temporal, _rec(1), _rec(2))
        rid = _row_ids(db_connection, 2)[2]
        r = client.post("/webhook/advisory/promote", json={"record_ids": [rid]})
        assert r.json()["queued"] == 1
        assert _started(temporal) == [f"advisory-promote-{rid}"]

    def test_status_returns_counts(self, client, db_connection, temporal):
        self._seed(client, temporal, _rec(1), _rec(2, published=0, status="Draft"))
        r = client.get("/webhook/advisory/status")
        assert r.status_code == 200
        assert r.json() == {"total": 2, "published": 1, "indexed": 0, "unindexed": 2, "promoted": 0}

    def test_endpoints_require_a_signed_in_admin(self, db_connection, temporal, monkeypatch):
        from fastapi.testclient import TestClient
        from pipeline import api

        env = {
            "AUTH_DISABLED": "false",
            "KEYCLOAK_ISSUER": "https://example.com/realms/test",
            "KEYCLOAK_JWKS_URL": "https://example.com/realms/test/protocol/openid-connect/certs",
        }
        with patch.dict(os.environ, env):
            c = TestClient(api.app)
            assert c.post("/webhook/advisory/reindex", json={}).status_code == 401
            assert c.post("/webhook/advisory/promote", json={}).status_code == 401
            assert c.get("/webhook/advisory/status").status_code == 401
