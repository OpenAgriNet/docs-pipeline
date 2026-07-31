"""Tests for Master Scheme Catalog (AI tool / prompt sync)."""

from __future__ import annotations

import json
import os

import pytest


@pytest.fixture
def catalog_db(temp_db_path):
    from pipeline import db
    from pipeline import scheme_catalog

    db.DB_PATH = temp_db_path
    db.init_db()
    scheme_catalog.ensure_catalog_schema()
    yield db, scheme_catalog
    db.DB_PATH = os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db")


def test_bootstrap_seeds_13_vector_schemes(catalog_db):
    db, scheme_catalog = catalog_db
    result = scheme_catalog.bootstrap_catalog_if_empty()
    assert result["bootstrapped"] is True
    assert result["vector_count"] == 13
    snap = scheme_catalog.build_snapshot()
    codes = {s["scheme_code"] for s in snap["vector_schemes"]}
    assert len(codes) == 13
    assert "nbm" not in codes  # legacy-only
    assert "pkvy" in codes
    assert "pm-ddky" in codes
    assert snap["catalog_version"] >= 1
    assert "search_schemes_doc" in snap["tool_prompts"]
    assert "vector_schemes_bullets_en" in snap["tool_prompts"]
    assert snap["routing_exceptions"]["pkvy"] == "search_schemes"
    assert snap["routing_exceptions"]["nbm"] == "get_scheme_info"
    # second call is no-op
    again = scheme_catalog.bootstrap_catalog_if_empty()
    assert again["bootstrapped"] is False


def test_scheme_metadata_and_promote_rebuild(catalog_db):
    db, scheme_catalog = catalog_db
    scheme_catalog.bootstrap_catalog_if_empty()
    v0 = db.get_catalog_meta()["version"]

    db.upsert_document(
        workflow_id="wf-pkvy-1",
        document_id="doc-pkvy-1",
        filename="pkvy.pdf",
        filepath="/tmp/pkvy.pdf",
        stage="completed",
        page_count=10,
        chunk_count=5,
        instance="default",
    )
    result = scheme_catalog.apply_scheme_metadata(
        "wf-pkvy-1",
        document_kind="scheme",
        scheme_code="pkvy",
        scheme_name="Paramparagat Krishi Vikas Yojana",
        scheme_aliases=["PKVY", "organic"],
        network_visible=True,
        catalog_visible=True,
    )
    assert result["document"]["scheme_code"] == "pkvy"
    assert result["catalog_version"] is not None
    assert result["catalog_version"] > v0

    entry = db.get_catalog_entry("pkvy")
    assert entry is not None
    assert entry["status"] == "live"
    assert entry["source"] == "pipeline"
    aliases = json.loads(entry["scheme_aliases_json"])
    assert "organic" in aliases or "PKVY" in aliases


def test_code_rename_pending_reindex(catalog_db):
    db, scheme_catalog = catalog_db
    scheme_catalog.bootstrap_catalog_if_empty()
    db.upsert_document(
        workflow_id="wf-rename",
        document_id="doc-rename",
        filename="x.pdf",
        filepath="/tmp/x.pdf",
        stage="completed",
        chunk_count=3,
        instance="default",
    )
    scheme_catalog.apply_scheme_metadata(
        "wf-rename",
        document_kind="scheme",
        scheme_code="old-code",
        scheme_name="Old",
    )
    result = scheme_catalog.apply_scheme_metadata(
        "wf-rename",
        scheme_code="new-code",
        scheme_name="New",
    )
    assert result["pending_reindex"] is True
    doc = db.get_document("wf-rename")
    assert doc["scheme_code"] == "new-code"
    assert int(doc["reindex_required"]) == 1
    # New code must not appear in live vector_schemes
    snap = scheme_catalog.build_snapshot()
    live_codes = {s["scheme_code"] for s in snap["vector_schemes"]}
    assert "new-code" not in live_codes
    entry = db.get_catalog_entry("new-code")
    assert entry is not None
    assert entry["status"] == "pending_reindex"


def test_prepare_records_scheme_payload():
    from pipeline.activities import _prepare_records

    chunks = [
        {
            "chunk_number": 1,
            "original_text": "Eligibility for farmers under the scheme.",
            "token_count": 10,
            "page_start": 1,
            "page_end": 1,
            "is_excluded": False,
        }
    ]
    records = _prepare_records(
        "doc1",
        "pkvy.pdf",
        chunks,
        workflow_id="wf1",
        instance="default",
        document_kind="scheme",
        scheme_code="pkvy",
        scheme_name="Paramparagat Krishi Vikas Yojana",
        scheme_aliases=["PKVY"],
    )
    assert len(records) == 1
    r = records[0]
    assert r["type"] == "scheme"
    assert r["scheme_code"] == "pkvy"
    assert r["scheme_name"].startswith("Paramparagat")
    assert r["scheme_aliases"] == ["PKVY"]
    assert r["chunk_id"] == r["_id"]
    assert r["chunk_index"] == 1


def test_prepare_records_document_unchanged():
    from pipeline.activities import _prepare_records

    chunks = [
        {
            "chunk_number": 0,
            "original_text": "General agronomy notes.",
            "token_count": 5,
            "is_excluded": False,
        }
    ]
    records = _prepare_records(
        "doc2",
        "notes.pdf",
        chunks,
        document_kind="document",
    )
    assert records[0]["type"] == "document"
    assert "scheme_code" not in records[0]


@pytest.fixture
def asgi_client(temp_db_path, mock_temporal_client, mock_minio_client):
    """HTTP client that does not run real Temporal/MinIO lifespan."""
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient
    from pipeline import api, db, scheme_catalog

    db.DB_PATH = temp_db_path
    db.init_db()
    api.temporal_client = mock_temporal_client
    api.minio_client = mock_minio_client
    scheme_catalog.ensure_catalog_schema()

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    original = api.app.router.lifespan_context
    api.app.router.lifespan_context = _noop_lifespan
    try:
        with TestClient(api.app) as client:
            yield client
    finally:
        api.app.router.lifespan_context = original
        db.DB_PATH = os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db")


def test_catalog_api_snapshot(asgi_client):
    from pipeline import scheme_catalog

    scheme_catalog.bootstrap_catalog_if_empty()

    r = asgi_client.get("/catalog/v1/version")
    assert r.status_code == 200
    assert "version" in r.json()
    assert r.json()["version"] >= 1

    r = asgi_client.get("/catalog/v1/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["api_version"] == "1"
    assert len(body["vector_schemes"]) == 13
    assert "tool_prompts" in body
    assert "legacy_schemes" in body
    assert body["collections"]["scheme_qdrant"]["name"] == "schemes-index"

    r = asgi_client.get("/catalog/v1/tool-prompt")
    assert r.status_code == 200
    assert "search_schemes_doc" in r.json()["tool_prompts"]

    r = asgi_client.get("/catalog/v1/schemes")
    assert r.status_code == 200
    assert len(r.json()["vector_schemes"]) == 13


def test_catalog_service_key_auth(asgi_client, monkeypatch):
    from pipeline import scheme_catalog

    scheme_catalog.bootstrap_catalog_if_empty()
    monkeypatch.setenv("CATALOG_SERVICE_API_KEYS", "test-secret-key")

    r = asgi_client.get(
        "/catalog/v1/snapshot",
        headers={"X-Catalog-Service-Key": "test-secret-key"},
    )
    assert r.status_code == 200
    # AUTH_DISABLED bypass user has admin — still allowed without key
    r2 = asgi_client.get("/catalog/v1/snapshot")
    assert r2.status_code == 200


def test_scheme_metadata_api(asgi_client):
    from pipeline import db

    db.upsert_document(
        workflow_id="wf-meta",
        document_id="doc-meta",
        filename="scheme.pdf",
        filepath="/tmp/scheme.pdf",
        stage="chunk_review",
        instance="default",
    )
    r = asgi_client.patch(
        "/documents/wf-meta/scheme-metadata",
        json={
            "document_kind": "scheme",
            "scheme_code": "mif",
            "scheme_name": "Micro Irrigation Fund",
            "scheme_aliases": ["MIF"],
            "network_visible": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scheme_code"] == "mif"
    assert body["document_kind"] == "scheme"
    assert "MIF" in body["scheme_aliases"]
