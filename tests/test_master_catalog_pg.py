"""Tests for the Postgres Master Catalog + Redis push (master_catalog_pg)."""

from __future__ import annotations

import json
import os

import pytest


class FakeCursor:
    def __init__(self, store: dict):
        self.store = store
        self._last_result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=None):
        sql_norm = " ".join(sql.split())
        if sql_norm.startswith("INSERT INTO master_catalog"):
            (code, content_type, name, tool_name, doc_id, prompt_snippet,
             aliases, status, workflow_id, instance, instance_name,
             updated_at) = params
            existing = self.store.setdefault("rows", {}).get(code)
            # Mirrors the real ON CONFLICT: status accumulates tiers, never overwrites.
            merged_status = sorted({*(existing["status"] if existing else []), status})
            self.store["rows"][code] = {
                "code": code,
                "content_type": content_type,
                "name": name,
                "tool_name": tool_name,
                "doc_id": doc_id,
                "prompt_snippet": prompt_snippet,
                "aliases": aliases,
                "status": merged_status,
                "workflow_id": workflow_id,
                "instance": instance,
                "instance_name": instance_name,
                "updated_at": updated_at,
            }
            self._last_result = None
        elif sql_norm.startswith("UPDATE master_catalog_meta"):
            self.store["version"] = self.store.get("version", 0) + 1
            self._last_result = (self.store["version"],)
        elif sql_norm.startswith("SELECT * FROM master_catalog WHERE status"):
            statuses = set(params[0])
            rows = [r for r in self.store.get("rows", {}).values() if statuses & set(r["status"])]
            self._last_result = sorted(rows, key=lambda r: r["code"])
        elif sql_norm.startswith("CREATE TABLE") or "INSERT INTO master_catalog_meta" in sql_norm:
            self._last_result = None
        else:
            raise AssertionError(f"Unexpected SQL in FakeCursor: {sql_norm}")

    def fetchone(self):
        return self._last_result

    def fetchall(self):
        return self._last_result or []


class FakeConnection:
    def __init__(self, store: dict):
        self.store = store
        self.closed = False

    def cursor(self, cursor_factory=None):
        return FakeCursor(self.store)

    def close(self):
        self.closed = True


class FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttls[key] = ex


@pytest.fixture
def catalog_pg(monkeypatch, temp_db_path):
    from pipeline import db
    from pipeline import master_catalog_pg as pg

    db.DB_PATH = temp_db_path
    db.init_db()

    store: dict = {}
    fake_conn = FakeConnection(store)
    monkeypatch.setattr(pg, "get_pg_connection", lambda: fake_conn)

    fake_redis = FakeRedis()
    monkeypatch.setattr(pg, "get_redis_client", lambda: fake_redis)

    yield db, pg, store, fake_redis

    db.DB_PATH = os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db")


def _make_scheme_doc(db, workflow_id="wf-1", scheme_code="pm-ddky", tool_routing="qdrant", aliases=None, instance="default"):
    db.upsert_document(
        workflow_id=workflow_id,
        document_id=f"doc-{workflow_id}",
        filename="scheme.pdf",
        filepath="/tmp/scheme.pdf",
        stage="approval_for_prod",
        page_count=5,
        chunk_count=3,
        instance=instance,
    )
    db.update_document_fields(
        workflow_id,
        document_kind="scheme",
        scheme_code=scheme_code,
        scheme_name="Prime Minister Dhan-Dhaanya Krishi Yojana",
        tool_routing=tool_routing,
        scheme_aliases_json=json.dumps(aliases) if aliases is not None else None,
    )


def test_resolve_tool_name():
    from pipeline.master_catalog_pg import _resolve_tool_name

    assert _resolve_tool_name("legacy") == "get_scheme_info"
    assert _resolve_tool_name("qdrant") == "search_schemes"
    assert _resolve_tool_name("both") == "search_schemes"
    assert _resolve_tool_name(None) == "search_schemes"


def test_generate_prompt_snippet():
    from pipeline.master_catalog_pg import _generate_prompt_snippet

    assert (
        _generate_prompt_snippet("Micro Irrigation Fund", "mif", "scheme")
        == "Refer to 'Micro Irrigation Fund' (code: mif) for guidance on this government scheme."
    )
    assert (
        _generate_prompt_snippet("Weekly advisory", "wa", "advisory")
        == "Refer to 'Weekly advisory' (code: wa) for guidance on this advisory."
    )


def test_sync_catalog_entry_rejects_bad_status(catalog_pg):
    db, pg, store, fake_redis = catalog_pg
    with pytest.raises(ValueError):
        pg.sync_catalog_entry("wf-1", "prod")


def test_sync_catalog_entry_skips_missing_document(catalog_pg):
    db, pg, store, fake_redis = catalog_pg
    assert pg.sync_catalog_entry("does-not-exist", "dev") is None
    assert store.get("rows") is None


def test_sync_catalog_entry_syncs_non_scheme_document_kind(catalog_pg):
    db, pg, store, fake_redis = catalog_pg
    db.upsert_document(
        workflow_id="wf-doc-only",
        document_id="doc-1",
        filename="general.pdf",
        filepath="/tmp/general.pdf",
        stage="completed",
        page_count=1,
        chunk_count=1,
        instance="default",
    )
    # document_kind defaults to "document" — still synced, no eligibility gate
    version = pg.sync_catalog_entry("wf-doc-only", "dev")
    assert version == 1
    assert "doc-1" in store["rows"]
    # No scheme_name — falls back to filename, not the bare code, so the
    # prompt snippet is still readable.
    assert store["rows"]["doc-1"]["name"] == "general.pdf"


def test_sync_catalog_entry_falls_back_to_doc_id_when_no_scheme_code(catalog_pg):
    db, pg, store, fake_redis = catalog_pg
    db.upsert_document(
        workflow_id="wf-no-code",
        document_id="doc-2",
        filename="scheme.pdf",
        filepath="/tmp/scheme.pdf",
        stage="approval_for_prod",
        page_count=1,
        chunk_count=1,
        instance="default",
    )
    db.update_document_fields("wf-no-code", document_kind="scheme")
    version = pg.sync_catalog_entry("wf-no-code", "dev")
    assert version == 1
    assert "doc-2" in store["rows"]


def test_sync_catalog_entry_dev_upserts_and_pushes_redis(catalog_pg):
    db, pg, store, fake_redis = catalog_pg
    _make_scheme_doc(db, workflow_id="wf-1", scheme_code="pm-ddky", tool_routing="qdrant")

    version = pg.sync_catalog_entry("wf-1", "dev")
    assert version == 1

    row = store["rows"]["pm-ddky"]
    assert row["content_type"] == "scheme"
    assert row["name"] == "Prime Minister Dhan-Dhaanya Krishi Yojana"
    assert row["tool_name"] == "search_schemes"
    assert row["doc_id"] == "doc-wf-1"
    assert row["prompt_snippet"] == (
        "Refer to 'Prime Minister Dhan-Dhaanya Krishi Yojana' (code: pm-ddky) "
        "for guidance on this government scheme."
    )
    assert row["status"] == ["dev"]

    dev_snapshot = json.loads(fake_redis.data["master-catalog:dev:snapshot"])
    assert dev_snapshot["version"] == 1
    codes = {e["code"] for e in dev_snapshot["entries"]}
    assert codes == {"pm-ddky"}

    live_snapshot = json.loads(fake_redis.data["master-catalog:live:snapshot"])
    assert live_snapshot["entries"] == []  # not promoted yet

    assert fake_redis.ttls["master-catalog:dev:snapshot"] == 172800


def test_sync_catalog_entry_live_appears_in_both_tiers(catalog_pg):
    db, pg, store, fake_redis = catalog_pg
    _make_scheme_doc(db, workflow_id="wf-2", scheme_code="mif", tool_routing="legacy")

    pg.sync_catalog_entry("wf-2", "dev")
    pg.sync_catalog_entry("wf-2", "live")

    assert store["rows"]["mif"]["status"] == ["dev", "live"]
    assert store["rows"]["mif"]["tool_name"] == "get_scheme_info"

    dev_snapshot = json.loads(fake_redis.data["master-catalog:dev:snapshot"])
    live_snapshot = json.loads(fake_redis.data["master-catalog:live:snapshot"])
    assert {e["code"] for e in dev_snapshot["entries"]} == {"mif"}
    assert {e["code"] for e in live_snapshot["entries"]} == {"mif"}


def test_sync_catalog_entry_dev_resync_after_live_does_not_regress_status(catalog_pg):
    """A reingest-to-dev after promotion must not silently drop the entry from
    the live snapshot — status accumulates tiers, it doesn't get overwritten."""
    db, pg, store, fake_redis = catalog_pg
    _make_scheme_doc(db, workflow_id="wf-5", scheme_code="nfsm")

    pg.sync_catalog_entry("wf-5", "dev")
    pg.sync_catalog_entry("wf-5", "live")
    pg.sync_catalog_entry("wf-5", "dev")  # e.g. a later reingest to DEV

    assert store["rows"]["nfsm"]["status"] == ["dev", "live"]
    live_snapshot = json.loads(fake_redis.data["master-catalog:live:snapshot"])
    assert {e["code"] for e in live_snapshot["entries"]} == {"nfsm"}


def test_sync_catalog_entry_upsert_no_duplicates(catalog_pg):
    db, pg, store, fake_redis = catalog_pg
    _make_scheme_doc(db, workflow_id="wf-3", scheme_code="cdp")

    pg.sync_catalog_entry("wf-3", "dev")
    pg.sync_catalog_entry("wf-3", "dev")

    assert len(store["rows"]) == 1
    dev_snapshot = json.loads(fake_redis.data["master-catalog:dev:snapshot"])
    assert len(dev_snapshot["entries"]) == 1


def test_sync_catalog_entry_stores_aliases(catalog_pg):
    db, pg, store, fake_redis = catalog_pg
    _make_scheme_doc(
        db, workflow_id="wf-6", scheme_code="mif",
        aliases=["MIF", "micro irrigation fund"],
    )

    pg.sync_catalog_entry("wf-6", "dev")

    # Curator aliases are preserved and lead, then bootstrap/derived ones are
    # merged in so a scheme is never searchable by its exact name alone.
    stored = store["rows"]["mif"]["aliases"]
    assert stored[:2] == ["MIF", "micro irrigation fund"]
    assert len(stored) > 2
    lowered = {a.lower() for a in stored}
    assert "micro irrigation fund scheme" in lowered  # from the bootstrap list
    assert "mif" in lowered

    dev_snapshot = json.loads(fake_redis.data["master-catalog:dev:snapshot"])
    entry = next(e for e in dev_snapshot["entries"] if e["code"] == "mif")
    assert entry["aliases"] == stored


def test_sync_catalog_entry_stores_instance_name(catalog_pg):
    """The readable state name rides along with the instance code."""
    db, pg, store, fake_redis = catalog_pg
    _make_scheme_doc(db, workflow_id="wf-inst", scheme_code="mif-2", instance="mh")

    pg.sync_catalog_entry("wf-inst", "dev")

    assert store["rows"]["mif-2"]["instance"] == "mh"
    assert store["rows"]["mif-2"]["instance_name"] == "Maharashtra"
    dev_snapshot = json.loads(fake_redis.data["master-catalog:dev:snapshot"])
    entry = next(e for e in dev_snapshot["entries"] if e["code"] == "mif-2")
    assert entry["instance_name"] == "Maharashtra"


def test_vector_schemes_prompt_block_excludes_legacy_schemes(catalog_pg):
    """The dynamic prompt block must only ever be built from search_schemes
    (vector-indexed) entries — legacy get_scheme_info schemes stay a
    separate, hardcoded list on the AI-layer side and must never leak in."""
    db, pg, store, fake_redis = catalog_pg
    _make_scheme_doc(
        db, workflow_id="wf-7", scheme_code="pm-ddky", tool_routing="qdrant",
        aliases=["PM-DDKY", "dhan-dhaanya"],
    )
    _make_scheme_doc(
        db, workflow_id="wf-8", scheme_code="kcc", tool_routing="legacy",
    )

    pg.sync_catalog_entry("wf-7", "dev")
    pg.sync_catalog_entry("wf-8", "dev")

    dev_snapshot = json.loads(fake_redis.data["master-catalog:dev:snapshot"])
    prompt = dev_snapshot["prompt"]
    assert prompt["vector_scheme_count"] == 1
    assert "pm-ddky" in prompt["vector_schemes_bullets"]
    assert "kcc" not in prompt["vector_schemes_bullets"]
    assert prompt["vector_schemes_bullets"] == "- **Prime Minister Dhan-Dhaanya Krishi Yojana** (pm-ddky)"
    # The stored alias list is broad for recall, but the prompt is capped so a
    # long derived tail cannot bloat every AI request.
    identifiers = prompt["vector_schemes_identifiers"]
    assert identifiers.startswith("- `pm-ddky` / PM-DDKY / dhan-dhaanya")
    assert identifiers.count(" / ") <= pg._PROMPT_ALIAS_LIMIT
    assert len(store["rows"]["pm-ddky"]["aliases"]) > pg._PROMPT_ALIAS_LIMIT


def test_sync_catalog_entry_without_redis_host_skips_push_but_still_upserts(catalog_pg, monkeypatch):
    db, pg, store, fake_redis = catalog_pg
    monkeypatch.setattr(pg, "get_redis_client", lambda: None)
    _make_scheme_doc(db, workflow_id="wf-4", scheme_code="pkvy")

    version = pg.sync_catalog_entry("wf-4", "dev")
    assert version == 1
    assert "pkvy" in store["rows"]
