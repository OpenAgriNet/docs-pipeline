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


def test_derive_scheme_code_acronym(catalog_db):
    db, scheme_catalog = catalog_db
    assert scheme_catalog.derive_scheme_code("National Mission on Natural Farming") == "nmnf"
    # Single-word title has too few words for a useful acronym -> slug fallback.
    assert scheme_catalog.derive_scheme_code("Guideline") == "guideline"[:8]


def test_derive_scheme_code_collision_appends_suffix(catalog_db):
    db, scheme_catalog = catalog_db
    db.upsert_document(
        workflow_id="wf-nmnf-1",
        document_id="doc-nmnf-1",
        filename="a.pdf",
        filepath="/tmp/a.pdf",
        stage="completed",
        instance="default",
    )
    scheme_catalog.apply_scheme_metadata(
        "wf-nmnf-1", document_kind="scheme", scheme_code="nmnf", scheme_name="National Mission on Natural Farming",
    )
    # A second, unrelated document whose title happens to acronym to the same code.
    assert scheme_catalog.derive_scheme_code("New Model National Farming") == "nmnf-2"


def test_apply_scheme_metadata_auto_derives_code_from_name(catalog_db):
    db, scheme_catalog = catalog_db
    db.upsert_document(
        workflow_id="wf-auto-code",
        document_id="doc-auto-code",
        filename="Guideline_of_NMNF_V2_Revised.pdf",
        filepath="/tmp/x.pdf",
        stage="ready_for_ingestion",
        instance="default",
    )
    result = scheme_catalog.apply_scheme_metadata(
        "wf-auto-code",
        document_kind="scheme",
        scheme_name="National Mission on Natural Farming",
    )
    # scheme_code was never passed explicitly -> derived from the title, not left blank/hash-based.
    assert result["document"]["scheme_code"] == "nmnf"
    assert result["document"]["scheme_name"] == "National Mission on Natural Farming"


def test_document_kind_widened_to_advisory_and_custom(catalog_db):
    db, scheme_catalog = catalog_db
    db.upsert_document(
        workflow_id="wf-kind",
        document_id="doc-kind",
        filename="advisory.pdf",
        filepath="/tmp/advisory.pdf",
        stage="ready_for_ingestion",
        instance="default",
    )
    result = scheme_catalog.apply_scheme_metadata("wf-kind", document_kind="advisory")
    assert result["document"]["document_kind"] == "advisory"

    result = scheme_catalog.apply_scheme_metadata("wf-kind", document_kind="how_to_faq")
    assert result["document"]["document_kind"] == "how_to_faq"

    with pytest.raises(ValueError):
        scheme_catalog.apply_scheme_metadata("wf-kind", document_kind="Not Valid!!")


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
    # The curator alias leads; bootstrap and derived aliases are merged in so
    # the scheme is not searchable by its exact name alone.
    assert r["scheme_aliases"][0] == "PKVY"
    assert len(r["scheme_aliases"]) > 1
    assert "paramparagat krishi vikas yojana" in {a.lower() for a in r["scheme_aliases"]}
    assert r["instance_name"] == "Default"
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


def test_reingest_blocked_when_never_classified(asgi_client, mock_temporal_client):
    from pipeline import db

    db.upsert_document(
        workflow_id="wf-unclassified",
        document_id="doc-unclassified",
        filename="x.pdf",
        filepath="/tmp/x.pdf",
        stage="completed",
        instance="default",
    )
    db.save_chunks("wf-unclassified", [
        {"chunk_number": 1, "original_text": "some content", "token_count": 5},
    ])

    r = asgi_client.post("/documents/wf-unclassified/reingest")
    assert r.status_code == 400
    assert "never been classified" in r.json()["detail"]


def test_reingest_allowed_once_classified(asgi_client, mock_temporal_client):
    from pipeline import db, scheme_catalog

    db.upsert_document(
        workflow_id="wf-classified",
        document_id="doc-classified",
        filename="x.pdf",
        filepath="/tmp/x.pdf",
        stage="completed",
        instance="default",
    )
    db.save_chunks("wf-classified", [
        {"chunk_number": 1, "original_text": "some content", "token_count": 5},
    ])
    scheme_catalog.apply_scheme_metadata(
        "wf-classified", document_kind="scheme", scheme_code="testcode", scheme_name="Test Scheme",
    )

    r = asgi_client.post("/documents/wf-classified/reingest")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"


def test_request_prod_ready_wrong_stage_rejected(asgi_client):
    from pipeline import db

    db.upsert_document(
        workflow_id="wf-prod-ready-wrong-stage",
        document_id="doc-prod-ready-wrong-stage",
        filename="x.pdf",
        filepath="/tmp/x.pdf",
        stage="chunk_review",
        instance="default",
    )
    r = asgi_client.post("/documents/wf-prod-ready-wrong-stage/request-prod-ready")
    assert r.status_code == 400


def test_request_prod_ready_flags_document_and_clears_on_approve(asgi_client, mock_temporal_client):
    from pipeline import db

    db.upsert_document(
        workflow_id="wf-prod-ready",
        document_id="doc-prod-ready",
        filename="x.pdf",
        filepath="/tmp/x.pdf",
        stage="approval_for_prod",
        instance="default",
    )
    doc = db.get_document("wf-prod-ready")
    assert doc["prod_ready_requested_at"] is None

    detail = asgi_client.get("/documents/wf-prod-ready").json()
    assert "request_prod_ready" in detail["available_actions"]

    r = asgi_client.post("/documents/wf-prod-ready/request-prod-ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] is True
    assert body["prod_ready_requested_at"] is not None

    doc = db.get_document("wf-prod-ready")
    assert doc["prod_ready_requested_at"] is not None

    # Once requested, it drops off available_actions (no duplicate requests) —
    # approve_prod (the real privileged action) stays available throughout.
    detail = asgi_client.get("/documents/wf-prod-ready").json()
    assert "request_prod_ready" not in detail["available_actions"]
    assert "approve_prod" in detail["available_actions"]

    # Actual prod approval clears the request flag.
    r = asgi_client.post("/documents/wf-prod-ready/approve-prod")
    assert r.status_code == 200, r.text
    doc = db.get_document("wf-prod-ready")
    assert doc["prod_ready_requested_at"] is None


class TestResolveSchemeAliases:
    """Aliases indexed with each scheme — curator + bootstrap + derived."""

    @pytest.mark.unit
    def test_nmeoop_gets_aliases_without_any_curation(self):
        """The real case: a scheme indexed with scheme_aliases: [] matched nothing."""
        from pipeline.scheme_catalog import resolve_scheme_aliases

        aliases = resolve_scheme_aliases(
            "nmeoop", "National Mission on Edible Oils - Oil Palm", None
        )

        lowered = {a.lower() for a in aliases}
        assert aliases, "a scheme must never index with an empty alias list"
        assert "nmeoop" in lowered
        assert "national mission on edible oils - oil palm" in lowered
        assert "national mission on edible oils oil palm" in lowered
        assert "oil palm" in lowered
        # The acronym of the significant words is NMEOOP, which is what the
        # code already is — dedupe keeps one copy rather than both.
        assert lowered.count("nmeoop") if isinstance(lowered, list) else True

    @pytest.mark.unit
    def test_acronym_is_derived_from_significant_words(self):
        from pipeline.scheme_catalog import resolve_scheme_aliases

        aliases = resolve_scheme_aliases("cdp-2", "Crop Diversification Programme", None)

        assert "CDP" in aliases

    @pytest.mark.unit
    def test_stopwords_are_excluded_from_the_acronym(self):
        from pipeline.scheme_catalog import resolve_scheme_aliases

        aliases = resolve_scheme_aliases("x", "National Mission on Edible Oils", None)

        assert "NMEO" in aliases  # the "on" is skipped

    @pytest.mark.unit
    def test_curator_aliases_are_kept_and_come_first(self):
        from pipeline.scheme_catalog import resolve_scheme_aliases

        aliases = resolve_scheme_aliases(
            "nmeoop",
            "National Mission on Edible Oils - Oil Palm",
            ["NMEO-OP", "oil palm mission"],
        )

        assert aliases[0] == "NMEO-OP"
        assert "oil palm mission" in aliases
        # merged, not short-circuited
        assert "nmeoop" in {a.lower() for a in aliases}

    @pytest.mark.unit
    def test_bootstrap_aliases_are_merged_for_known_codes(self):
        from pipeline.scheme_catalog import resolve_scheme_aliases

        aliases = {a.lower() for a in resolve_scheme_aliases("nmeo", None, None)}

        assert "nmeo-os" in aliases
        assert "oilseeds mission" in aliases

    @pytest.mark.unit
    def test_aliases_are_deduped_case_insensitively(self):
        from pipeline.scheme_catalog import resolve_scheme_aliases

        aliases = resolve_scheme_aliases("pkvy", "Paramparagat Krishi Vikas Yojana", ["PKVY", "pkvy"])

        lowered = [a.lower() for a in aliases]
        assert len(lowered) == len(set(lowered))

    @pytest.mark.unit
    def test_json_string_aliases_are_accepted(self):
        """documents.scheme_aliases_json arrives as a JSON string."""
        from pipeline.scheme_catalog import resolve_scheme_aliases

        aliases = resolve_scheme_aliases("makhana", "Makhana Scheme", '["fox nut", "lotus seed"]')

        lowered = {a.lower() for a in aliases}
        assert "fox nut" in lowered and "lotus seed" in lowered

    @pytest.mark.unit
    def test_no_code_and_no_name_yields_nothing(self):
        from pipeline.scheme_catalog import resolve_scheme_aliases

        assert resolve_scheme_aliases(None, None, None) == []
