"""
Master Catalog (Postgres) — prompt/tool metadata for the AI layer.

Generic across content types (scheme, advisory, or whatever else eventually
carries its own code/name/tool routing) — not scheme-specific by name.
Every document gets synced regardless of document_kind; code falls back to
doc_id/workflow_id when scheme_code isn't set. Separate from
`scheme_catalog.py` (SQLite `scheme_catalog_entries`, which tracks PROD
Qdrant publication state for search, and is legitimately scheme-only).

  - DEV ingest complete   -> sync_catalog_entry(workflow_id, status="dev")
  - PROD promote complete -> sync_catalog_entry(workflow_id, status="live")

Each row is keyed on `code` (upsert, never duplicated). `status` is a TEXT[]
that accumulates every tier a document has synced under ({dev} then
{dev,live}) rather than being overwritten — a later DEV re-sync (e.g. a
reingest after promotion) must not silently regress an already-live entry
back out of the live snapshot. Redis snapshot keys are
`master-catalog:dev:snapshot` (rows whose status overlaps {dev, live} — dev
testers should also see promoted entries) and `master-catalog:live:snapshot`
(rows whose status contains live, for prod). Both are refreshed on every
sync so they never drift relative to each other.

Each snapshot also carries a pre-rendered `prompt` block (vector_schemes_
bullets/identifiers, built from tool_name == "search_schemes" entries only)
so the AI layer can splice a live scheme list straight into its system
prompt instead of hand-maintaining one per language. Legacy schemes
(tool_name == "get_scheme_info") are intentionally excluded from that block
— they stay a separate, hardcoded list on the AI-layer side.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

from . import db
from .scheme_catalog import parse_aliases

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS master_catalog (
    code            TEXT PRIMARY KEY,
    content_type    TEXT NOT NULL DEFAULT 'scheme',
    name            TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    doc_id          TEXT,
    prompt_snippet  TEXT,
    aliases         TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    status          TEXT[] NOT NULL DEFAULT ARRAY['dev']::TEXT[],
    workflow_id     TEXT,
    instance        TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS master_catalog_meta (
    id INTEGER PRIMARY KEY DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO master_catalog_meta (id, version)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

-- Migrate pre-existing installs where status was a scalar TEXT column: a
-- promote-to-live sync used to overwrite it outright, so a later DEV
-- re-sync (e.g. reingest) would silently regress a live entry back to dev
-- and drop it from the live snapshot. Now it accumulates tiers instead.
DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
        WHERE table_name = 'master_catalog' AND column_name = 'status') = 'text' THEN
        ALTER TABLE master_catalog ALTER COLUMN status DROP DEFAULT;
        ALTER TABLE master_catalog ALTER COLUMN status TYPE TEXT[] USING ARRAY[status]::TEXT[];
        ALTER TABLE master_catalog ALTER COLUMN status SET DEFAULT ARRAY['dev']::TEXT[];
    END IF;
END $$;

ALTER TABLE master_catalog ADD COLUMN IF NOT EXISTS aliases TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[];
"""

_REDIS_KEY_PREFIX = "master-catalog"


def _pg_dsn() -> str:
    return (
        f"host={os.environ.get('MASTER_CATALOG_PG_HOST', 'localhost')} "
        f"port={os.environ.get('MASTER_CATALOG_PG_PORT', '5432')} "
        f"dbname={os.environ.get('MASTER_CATALOG_PG_DB', 'master_catalog')} "
        f"user={os.environ.get('MASTER_CATALOG_PG_USER', 'master_catalog')} "
        f"password={os.environ.get('MASTER_CATALOG_PG_PASSWORD', '')} "
        f"sslmode={os.environ.get('MASTER_CATALOG_PG_SSLMODE', 'disable')}"
    )


def get_pg_connection():
    """New connection per call — sync calls are rare (ingest/promote events only), not per-request."""
    conn = psycopg2.connect(_pg_dsn())
    conn.autocommit = True
    return conn


def get_redis_client():
    import redis  # local import: keep this optional dep out of the module import path for tests

    host = os.environ.get("AI_LAYER_REDIS_HOST", "").strip()
    if not host:
        return None
    return redis.Redis(
        host=host,
        port=int(os.environ.get("AI_LAYER_REDIS_PORT", "6379")),
        db=int(os.environ.get("AI_LAYER_REDIS_DB", "0")),
        password=os.environ.get("AI_LAYER_REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )


def _redis_ttl_seconds() -> int:
    return int(os.environ.get("MASTER_CATALOG_REDIS_TTL_SECONDS", "172800"))


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_tool_name(tool_routing: Optional[str]) -> str:
    """Which existing AI-layer tool this entry routes to — a label, not code."""
    routing = (tool_routing or "qdrant").strip().lower()
    return "get_scheme_info" if routing == "legacy" else "search_schemes"


def _generate_prompt_snippet(name: str, code: str, content_type: str) -> str:
    """A single sentence meant to be dropped directly into an agent system
    prompt as a reference entry — not a markdown bullet. There's no document
    summary/description captured during ingestion to draw a real topic from,
    so this stays generic (name + code + content type) rather than fabricate
    one; `_build_vector_schemes_prompt_block` below is what actually renders
    the searchable scheme list callers use, this field is a per-row preview."""
    kind = (content_type or "document").strip().lower()
    label = "government scheme" if kind == "scheme" else kind
    return f"Refer to '{name}' (code: {code}) for guidance on this {label}."


def sync_catalog_entry(workflow_id: str, status: str) -> Optional[int]:
    """
    Upsert this document's catalog metadata into the Postgres master catalog
    and refresh both Redis snapshot tiers.

    status: "dev" (DEV ingest complete) | "live" (PROD promote complete)
    Returns the new catalog version, or None if the document doesn't exist
    (nothing to sync).
    """
    if status not in ("dev", "live"):
        raise ValueError("status must be 'dev' or 'live'")

    doc = db.get_document(workflow_id)
    if not doc:
        logger.info("master_catalog_pg: %s not found, skipping sync", workflow_id)
        return None

    content_type = (doc.get("document_kind") or "document").strip().lower()

    # code is the primary key — fall back to doc_id/workflow_id when this
    # document has no scheme_code so every document still gets a row.
    code = (
        (doc.get("scheme_code") or "").strip().lower()
        or (doc.get("document_id") or "").strip().lower()
        or workflow_id.strip().lower()
    )

    # Same fallback chain as the UI's getDocumentListLabel (ui/src/lib/pipelineUi.js)
    # so a non-scheme document still gets a readable name instead of the bare code.
    name = (
        (doc.get("scheme_name") or "").strip()
        or (doc.get("display_name") or "").strip()
        or (doc.get("filename") or "").strip()
        or (doc.get("source_filename") or "").strip()
        or code
    )
    tool_name = _resolve_tool_name(doc.get("tool_routing"))
    prompt_snippet = _generate_prompt_snippet(name, code, content_type)
    aliases = parse_aliases(doc.get("scheme_aliases_json"))
    doc_id = doc.get("document_id")
    instance = (doc.get("instance") or "default").strip().lower()
    now = _utc_now_iso()

    conn = get_pg_connection()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO master_catalog
                    (code, content_type, name, tool_name, doc_id, prompt_snippet,
                     aliases, status, workflow_id, instance, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, ARRAY[%s]::TEXT[], %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET
                    content_type = EXCLUDED.content_type,
                    name = EXCLUDED.name,
                    tool_name = EXCLUDED.tool_name,
                    doc_id = EXCLUDED.doc_id,
                    prompt_snippet = EXCLUDED.prompt_snippet,
                    aliases = EXCLUDED.aliases,
                    status = ARRAY(
                        SELECT DISTINCT unnest(master_catalog.status || EXCLUDED.status)
                    ),
                    workflow_id = EXCLUDED.workflow_id,
                    instance = EXCLUDED.instance,
                    updated_at = EXCLUDED.updated_at
                """,
                (code, content_type, name, tool_name, doc_id, prompt_snippet,
                 aliases, status, workflow_id, instance, now),
            )
            cur.execute(
                """
                UPDATE master_catalog_meta
                SET version = version + 1, updated_at = %s
                WHERE id = 1
                RETURNING version
                """,
                (now,),
            )
            row = cur.fetchone()
            version = row[0] if row else None

        logger.info(
            "master_catalog_pg: synced code=%s name=%s status=%s workflow_id=%s version=%s",
            code, name, status, workflow_id, version,
        )
        _push_snapshots_to_redis(conn, version, now)
        return version
    finally:
        conn.close()


def _fetch_rows(conn, tier: str) -> list[dict]:
    """tier='dev' includes dev+live rows (dev testers should also see promoted entries);
    tier='live' includes only live rows (prod). A row's `status` accumulates every
    tier it has ever synced under, so this is an array-overlap check, not equality."""
    statuses = ("dev", "live") if tier == "dev" else ("live",)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM master_catalog WHERE status && %s ORDER BY code",
            (list(statuses),),
        )
        return [dict(r) for r in cur.fetchall()]


def _row_to_json(row: dict) -> dict:
    updated_at = row.get("updated_at")
    return {
        "code": row["code"],
        "content_type": row.get("content_type"),
        "name": row["name"],
        "tool_name": row["tool_name"],
        "doc_id": row.get("doc_id"),
        "prompt_snippet": row.get("prompt_snippet"),
        "aliases": row.get("aliases") or [],
        "status": row.get("status"),
        "workflow_id": row.get("workflow_id"),
        "instance": row.get("instance"),
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
    }


def _build_vector_schemes_prompt_block(entries: list[dict]) -> dict:
    """Pre-rendered system-prompt fragments for the AI layer's dynamic
    "vector-indexed schemes" section (bharat-oan-api's search_schemes routing).

    Restricted to tool_name == "search_schemes": legacy get_scheme_info
    schemes are a separate, still-hardcoded list in the AI layer and must
    never be pulled into this block.
    """
    vector_entries = sorted(
        (e for e in entries if e.get("tool_name") == "search_schemes"),
        key=lambda e: e["code"],
    )
    bullets = []
    identifiers = []
    for e in vector_entries:
        code = e["code"]
        name = e["name"]
        bullets.append(f"- **{name}** ({code})")
        alias_suffix = "".join(f" / {a}" for a in (e.get("aliases") or []))
        identifiers.append(f"- `{code}`{alias_suffix}")
    return {
        "vector_schemes_bullets": "\n".join(bullets),
        "vector_schemes_identifiers": "\n".join(identifiers),
        "vector_scheme_count": len(vector_entries),
    }


def _push_snapshots_to_redis(conn, version: Optional[int], updated_at: str) -> None:
    """Best-effort: Postgres is the source of truth, Redis is a mirror. A failed
    push here doesn't fail the sync — the next catalog event repairs it."""
    try:
        client = get_redis_client()
        if client is None:
            logger.info("master_catalog_pg: AI_LAYER_REDIS_HOST unset, skipping Redis push")
            return
        ttl = _redis_ttl_seconds()
        for tier in ("dev", "live"):
            rows = _fetch_rows(conn, tier)
            entries = [_row_to_json(r) for r in rows]
            payload = {
                "version": version,
                "updated_at": updated_at,
                "status": tier,
                "entries": entries,
                "prompt": _build_vector_schemes_prompt_block(entries),
            }
            client.set(f"{_REDIS_KEY_PREFIX}:{tier}:snapshot", json.dumps(payload), ex=ttl)
            logger.info(
                "master_catalog_pg: pushed %s:%s snapshot (%d entries, version=%s)",
                _REDIS_KEY_PREFIX, tier, len(rows), version,
            )
    except Exception as exc:
        logger.warning("master_catalog_pg: Redis push failed: %s", exc)
