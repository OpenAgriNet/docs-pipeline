#!/usr/bin/env python3
"""
Re-key existing MinIO artifact objects under a per-tenant prefix.

Storage isolation (§5.3 of the tenant-isolation plan) prefixes every new
artifact key with its owning tenant: ``<instance>/<workflow_id>/<artifact_type>/
<filename>``. Objects written before that change live under the legacy
``documents/<workflow_id>/...`` layout (or an un-prefixed ``<workflow_id>/...``).
This operator tool copies each legacy object to its tenant-prefixed key, verifies
the copy, updates the SQLite ``document_artifacts.storage_uri`` to the new URI,
and (only with ``--delete-old``) removes the source object.

Ownership is always derived from SQLite: each ``document_artifacts`` row joins to
its ``documents`` row, which carries the authoritative ``instance``. Objects are
never guessed into a tenant.

⚠️  OPERATOR TOOL — NOT RUN AUTOMATICALLY
    - Dry-run by default. Nothing is copied/updated/deleted without ``--apply``.
    - ``--delete-old`` is additionally required to remove source objects; without
      it the old objects are left in place (safe, copy-only).
    - Reads remain correct throughout: the app resolves artifact URLs from the
      stored ``storage_uri``, so a half-finished run never breaks reads.
    - Take a backup / confirm you can re-ingest before using ``--delete-old``.

Usage:
    # Preview every tenant (no writes):
    python3 scripts/backfill_minio_prefix.py

    # Preview a single tenant:
    python3 scripts/backfill_minio_prefix.py --instance tenant-a

    # Apply the re-key (copy + update SQLite), keep old objects:
    python3 scripts/backfill_minio_prefix.py --instance tenant-a --apply

    # Apply and delete the source objects after verified copy:
    python3 scripts/backfill_minio_prefix.py --instance tenant-a --apply --delete-old
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _default_instance() -> str:
    return (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower() or "default"


def _normalize_instance(value: str | None) -> str:
    text = (value or "").strip().lower()
    return text or _default_instance()


def _safe_name(filename: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "artifact")


def _tenant_object_name(instance: str, workflow_id: str, artifact_type: str, filename: str) -> str:
    """Mirror pipeline.activities._minio_object_name (kept in sync deliberately)."""
    return f"{_normalize_instance(instance)}/{workflow_id}/{artifact_type}/{_safe_name(filename)}"


def _parse_minio_uri(uri: str) -> tuple[str, str] | None:
    """``minio://bucket/key`` -> ``(bucket, key)``; None if not a minio uri."""
    if not uri:
        return None
    normalized = uri
    if uri.startswith("minio:/") and not uri.startswith("minio://"):
        normalized = uri.replace("minio:/", "minio://", 1)
    if not normalized.startswith("minio://"):
        return None
    path = normalized[len("minio://"):]
    parts = path.split("/", 1)
    if len(parts) != 2 or not parts[1]:
        return None
    return parts[0], parts[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--db",
        default=os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db"),
        help="Path to the SQLite documents DB (default: DOCUMENT_DB_PATH).",
    )
    parser.add_argument(
        "--instance",
        default=None,
        help="Only re-key artifacts owned by this tenant (default: all tenants).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually copy objects and update SQLite (default: dry-run).",
    )
    parser.add_argument(
        "--delete-old",
        action="store_true",
        help="After a verified copy, delete the source object (requires --apply).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after N artifacts (0 = no limit). Useful for a staged rollout.",
    )
    return parser.parse_args()


def _load_artifacts(db_path: str, instance_filter: str | None) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT a.id AS artifact_id,
                   a.workflow_id,
                   a.artifact_type,
                   a.filename,
                   a.storage_uri,
                   d.instance AS instance
            FROM document_artifacts a
            JOIN documents d ON d.workflow_id = a.workflow_id
            ORDER BY a.id
            """
        ).fetchall()
    finally:
        conn.close()

    result: list[dict] = []
    for row in rows:
        inst = _normalize_instance(row["instance"])
        if instance_filter is not None and inst != _normalize_instance(instance_filter):
            continue
        result.append(dict(row))
    return result


def main() -> int:
    args = parse_args()
    if args.delete_old and not args.apply:
        print("--delete-old requires --apply; refusing to delete in dry-run.", file=sys.stderr)
        return 2

    if not os.path.exists(args.db):
        print(f"SQLite DB not found: {args.db}", file=sys.stderr)
        return 2

    artifacts = _load_artifacts(args.db, args.instance)
    print(f"Considering {len(artifacts)} artifact(s)"
          + (f" for tenant '{_normalize_instance(args.instance)}'" if args.instance else " across all tenants")
          + (" [DRY-RUN]" if not args.apply else " [APPLY]"))

    client = None
    conn = None
    if args.apply:
        from pipeline.activities import get_minio_client  # lazy: needs minio creds

        client = get_minio_client()
        conn = sqlite3.connect(args.db)

    planned = copied = updated = deleted = skipped = 0
    try:
        for art in artifacts:
            if args.limit and planned >= args.limit:
                break
            parsed = _parse_minio_uri(art["storage_uri"])
            if parsed is None:
                skipped += 1
                continue
            bucket, old_key = parsed
            inst = _normalize_instance(art["instance"])
            new_key = _tenant_object_name(inst, art["workflow_id"], art["artifact_type"], art["filename"] or "artifact")
            if old_key == new_key:
                skipped += 1
                continue

            planned += 1
            new_uri = f"minio://{bucket}/{new_key}"
            print(f"  [{art['artifact_id']}] {inst}: {old_key}  ->  {new_key}")

            if not args.apply:
                continue

            from minio.commonconfig import CopySource

            client.copy_object(bucket, new_key, CopySource(bucket, old_key))
            # Verify the copy exists before touching SQLite / deleting source.
            client.stat_object(bucket, new_key)
            copied += 1

            conn.execute(
                "UPDATE document_artifacts SET storage_uri = ? WHERE id = ?",
                (new_uri, art["artifact_id"]),
            )
            conn.commit()
            updated += 1

            if args.delete_old:
                client.remove_object(bucket, old_key)
                deleted += 1
    finally:
        if conn is not None:
            conn.close()

    print(
        f"\nPlanned re-keys: {planned} | copied: {copied} | sqlite updated: {updated} "
        f"| old deleted: {deleted} | skipped (already-prefixed / non-minio): {skipped}"
    )
    if not args.apply:
        print("Dry-run only — re-run with --apply to perform the re-key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
