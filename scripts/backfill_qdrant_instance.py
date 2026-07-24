#!/usr/bin/env python3
"""
Backfill the ``instance`` (tenant) payload on existing Qdrant points.

Multi-tenant search/read on the Qdrant backend AND-s an ``instance == <tenant>``
payload filter for every RESTRICTED caller (see ``_build_filter`` in
``pipeline/vector_store/qdrant_store.py``). New points get ``instance`` stamped by
ingest (and the upsert write-guard). Points that predate the multi-tenancy graft
carry NO ``instance`` payload, so a restricted default-tenant caller would filter
them out and see empty results until they are stamped.

This operator tool stamps the pre-graft points of a collection with their tenant
id so the collection can be read under the fail-closed instance filter. For the
single-tenant deployment the only collection is the default tenant's legacy
collection and every point belongs to ``DEFAULT_INSTANCE`` — the default mode
below handles exactly that. ``--per-document`` resolves each point's tenant from
SQLite (via its ``workflow_id`` payload) for collections that mix tenants.

Unrestricted/bypass callers (bh-main ``superadmin``) are never filtered, so this
backfill is only required before scoped (``state_admin``/``content_curator``/
``viewer``) users read a pre-graft collection.

⚠️  OPERATOR WARNING — READ BEFORE RUNNING
    - NOT run automatically. Run it deliberately, off-peak.
    - Idempotent: only points MISSING ``instance`` are touched (server-side
      ``IsEmpty`` filter), so re-runs are safe and cheap.
    - Take a snapshot / confirm you can reingest before mutating a live
      collection. Reingest from SQLite is available via
      ``scripts/bulk_reingest_sqlite_to_marqo.py`` (backend-agnostic path).
    - Always ``--dry-run`` first to see how many points would be stamped.

Usage:
    # single-tenant / default collection → stamp all unstamped points as DEFAULT_INSTANCE
    python3 scripts/backfill_qdrant_instance.py --dry-run
    python3 scripts/backfill_qdrant_instance.py

    # a specific collection + tenant
    python3 scripts/backfill_qdrant_instance.py --collection t-tenant-a-default --instance tenant-a

    # mixed collection → resolve each point's tenant from SQLite by workflow_id
    python3 scripts/backfill_qdrant_instance.py --collection documents-index --per-document
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _default_instance() -> str:
    return (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower() or "default"


def _default_collection() -> str:
    # Resolve the same way the app does (QDRANT_COLLECTION_NAME → …).
    from pipeline.vector_store import get_default_index_name

    return get_default_index_name()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--collection",
        default=None,
        help="Qdrant collection to backfill (default: the app's default collection).",
    )
    parser.add_argument(
        "--instance",
        default=_default_instance(),
        help="Instance id to stamp on points that have none (default: DEFAULT_INSTANCE). "
        "Ignored in --per-document mode.",
    )
    parser.add_argument(
        "--per-document",
        action="store_true",
        help="Resolve each point's tenant from SQLite by its workflow_id payload "
        "(use for collections that mix tenants). Points whose doc is unknown fall "
        "back to --instance.",
    )
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--dry-run", action="store_true", help="Report counts only; write nothing.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    from qdrant_client import models as qm

    from pipeline.vector_store.qdrant_store import get_qdrant_client

    collection = args.collection or _default_collection()
    client = get_qdrant_client()

    try:
        client.get_collection(collection)
    except Exception as exc:  # noqa: BLE001 - operator-facing tool
        print(f"ERROR: collection {collection!r} not found or unreachable: {exc}", file=sys.stderr)
        return 2

    # Only points MISSING the instance payload (idempotent).
    missing_filter = qm.Filter(must=[qm.IsEmptyCondition(is_empty=qm.PayloadField(key="instance"))])

    missing_count = client.count(collection, count_filter=missing_filter, exact=True).count
    total_count = client.count(collection, exact=True).count
    print(f"collection={collection} total_points={total_count} missing_instance={missing_count}")

    if missing_count == 0:
        print("Nothing to backfill — every point already carries an instance.")
        return 0

    if args.dry_run:
        mode = "per-document (from SQLite workflow_id)" if args.per_document else f"instance={args.instance!r}"
        print(f"[dry-run] would stamp {missing_count} point(s) using {mode}. No changes made.")
        return 0

    if not args.per_document:
        # Single server-side set_payload over the missing-instance filter.
        client.set_payload(
            collection_name=collection,
            payload={"instance": args.instance},
            points=missing_filter,
            wait=True,
        )
        print(f"Stamped {missing_count} point(s) with instance={args.instance!r}.")
        return 0

    # Per-document: page unstamped points, group by workflow_id, resolve from SQLite.
    from pipeline import db

    fallback = args.instance
    stamped = 0
    unknown = 0
    next_page = None
    doc_instance_cache: dict[str, str] = {}
    while True:
        points, next_page = client.scroll(
            collection_name=collection,
            scroll_filter=missing_filter,
            limit=args.batch_size,
            with_payload=True,
            with_vectors=False,
            offset=next_page,
        )
        if not points:
            break
        # bucket point-ids by resolved instance
        buckets: dict[str, list] = {}
        for p in points:
            payload = p.payload or {}
            wf = str(payload.get("workflow_id") or "")
            inst = doc_instance_cache.get(wf) if wf else None
            if inst is None:
                inst = fallback
                if wf:
                    try:
                        doc = db.get_document(wf)
                        if doc and doc.get("instance"):
                            inst = str(doc["instance"]).strip().lower()
                    except Exception:  # noqa: BLE001
                        pass
                    doc_instance_cache[wf] = inst
                if inst == fallback and not (wf and doc_instance_cache.get(wf) not in (None, fallback)):
                    unknown += 1
            buckets.setdefault(inst, []).append(p.id)
        for inst, ids in buckets.items():
            client.set_payload(collection_name=collection, payload={"instance": inst}, points=ids, wait=True)
            stamped += len(ids)
        if next_page is None:
            break
    print(f"Stamped {stamped} point(s) per-document ({unknown} fell back to instance={fallback!r}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
