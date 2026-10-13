"""Tests for POST /webhook (x-api-key protected)."""

import os
import uuid
from unittest.mock import patch

import pytest

TEST_KEY = "0123456789abcdef0123456789abcdef"

SAMPLE = [
    {
        "contentID": 101,
        "title": "Bacterial Leaf Blight in Rice",
        "domainName": "Agriculture",
        "status": "Published",
        "isPublished": 1,
        "slug": "101/bacterial-leaf-blight-in-rice",
        "createdAt": "2026-09-09T07:00:00.000Z",
        "updatedAt": "2026-09-09T07:00:00.000Z",
        "popCount": 1,
        "popList": [{"popID": 1001, "contentID": 101, "keywords": ["rice"]}],
    }
]

OK_BODY = {"success": True, "message": "Webhook processed successfully.", "recordCount": 1}


@pytest.fixture
def test_client(db_connection):
    """TestClient without lifespan (no Temporal/MinIO needed for /webhook)."""
    from fastapi.testclient import TestClient
    from pipeline import api

    return TestClient(api.app)


@pytest.fixture
def key_env():
    with patch.dict(os.environ, {"WEBHOOK_API_KEY": TEST_KEY}):
        yield


def _rows(db_connection):
    from pipeline import db

    with db.get_connection() as conn:
        return conn.execute("SELECT * FROM webhook_records").fetchall()


@pytest.mark.api
@pytest.mark.unit
class TestWebhookAuth:
    def test_missing_header_401(self, test_client, db_connection, key_env):
        assert test_client.post("/webhook", json=SAMPLE).status_code == 401

    def test_empty_header_401(self, test_client, db_connection, key_env):
        r = test_client.post("/webhook", json=SAMPLE, headers={"x-api-key": ""})
        assert r.status_code == 401

    def test_wrong_key_401(self, test_client, db_connection, key_env):
        r = test_client.post("/webhook", json=SAMPLE, headers={"x-api-key": "nope"})
        assert r.status_code == 401

    @pytest.mark.parametrize("value", [None, ""])
    def test_unconfigured_key_503_even_if_auth_disabled(self, test_client, db_connection, value):
        env = {"AUTH_DISABLED": "true"}
        if value is not None:
            env["WEBHOOK_API_KEY"] = value
        with patch.dict(os.environ, env):
            if value is None:
                os.environ.pop("WEBHOOK_API_KEY", None)
            r = test_client.post("/webhook", json=SAMPLE, headers={"x-api-key": "anything"})
        assert r.status_code == 503
        assert "WEBHOOK_API_KEY" in r.json()["detail"]


@pytest.mark.api
@pytest.mark.unit
class TestWebhookIngest:
    H = {"x-api-key": TEST_KEY}

    def test_valid_payload(self, test_client, db_connection, key_env):
        r = test_client.post("/webhook", json=SAMPLE, headers=self.H)
        assert r.status_code == 200
        assert r.json() == OK_BODY
        rows = _rows(db_connection)
        assert len(rows) == 1
        assert rows[0]["external_id"] == "101"
        assert uuid.UUID(rows[0]["id"]).version == 4
        assert rows[0]["category"] == "Agriculture"
        assert '"popList"' in rows[0]["payload"]

    def test_repost_is_idempotent(self, test_client, db_connection, key_env):
        test_client.post("/webhook", json=SAMPLE, headers=self.H)
        first_id = _rows(db_connection)[0]["id"]
        changed = [{**SAMPLE[0], "title": "New title"}]
        r = test_client.post("/webhook", json=changed, headers=self.H)
        assert r.json()["recordCount"] == 1
        rows = _rows(db_connection)
        assert len(rows) == 1 and rows[0]["title"] == "New title"
        assert rows[0]["id"] == first_id

    def test_two_records(self, test_client, db_connection, key_env):
        body = SAMPLE + [{**SAMPLE[0], "contentID": 102}]
        r = test_client.post("/webhook", json=body, headers=self.H)
        assert r.json()["recordCount"] == 2
        assert len(_rows(db_connection)) == 2

    def test_empty_array(self, test_client, db_connection, key_env):
        r = test_client.post("/webhook", json=[], headers=self.H)
        assert r.status_code == 200 and r.json()["recordCount"] == 0

    def test_non_array_422(self, test_client, db_connection, key_env):
        r = test_client.post("/webhook", json={"contentID": 1}, headers=self.H)
        assert r.status_code == 422

    def test_non_object_item_422(self, test_client, db_connection, key_env):
        r = test_client.post("/webhook", json=[1, 2], headers=self.H)
        assert r.status_code == 422

    def test_too_many_records_413(self, test_client, db_connection, key_env):
        from pipeline import api

        with patch.object(api, "WEBHOOK_MAX_RECORDS", 1):
            body = SAMPLE + [{**SAMPLE[0], "contentID": 102}]
            r = test_client.post("/webhook", json=body, headers=self.H)
        assert r.status_code == 413
        assert _rows(db_connection) == []
