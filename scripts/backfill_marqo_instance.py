#!/usr/bin/env python3
"""
Backfill the ``instance`` (tenant) field on existing Marqo vectors.

Multi-tenant search filtering AND-s in an ``instance IN (...)`` clause only when
the live Marqo index advertises a filterable ``instance`` field. New indexes
created via ``POST /admin/index/create`` already include it, and ingest stamps
every chunk. This operator tool stamps vectors that predate that change so a
legacy single-tenant index can be promoted to multi-tenant filtering.

⚠️  OPERATOR WARNING — READ BEFORE RUNNING
    - This is NOT run automatically. Run it deliberately, off-peak.
    - The live index may be shared with other consumers. Adding a filterable
      field to a Marqo *structured* index generally requires recreating the
      index (Marqo structured schemas are fixed at create time); in that case
      an in-place ``add_documents`` update of only ``instance`` will be rejected
      and you must instead recreate the index with the passage schema (which now
      includes ``instance``) and reingest — see
      ``scripts/bulk_reingest_sqlite_to_marqo.py``. For unstructured indexes an
      in-place update may work. Verify your index type first with
      ``GET /admin/index/schema``.
    - Always take a backup / confirm you can reingest before mutating a live index.

Usage:
    python3 scripts/backfill_marqo_instance.py --index documents-index --instance tenant-a
    python3 scripts/backfill_marqo_instance.py --index documents-index          # uses DEFAULT_INSTANCE
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import marqo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _default_instance() -> str:
    return (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower() or "default"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marqo-url", default=os.environ.get("MARQO_URL", "http://localhost:8882"))
    parser.add_argument(
        "--index",
        default=os.environ.get("MARQO_INDEX_NAME", "documents-index"),
        help="Marqo index to backfill",
    )
    parser.add_argument(
        "--instance",
        default=_default_instance(),
        help="Instance id to stamp on vectors that have none (default: DEFAULT_INSTANCE)",
    )
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Only stamp vectors that currently lack an instance value",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to Marqo",
    )
    return parser.parse_args()


def _index_has_instance_field(index) -> bool:
    try:
        settings = index.get_settings()
    except Exception:
        return False
    return "instance" in {
        f.get("name")
        for f in (settings.get("allFields") or [])
        if isinstance(f, dict) and f.get("name")
    }


def main() -> int:
    args = parse_args()
    instance = (args.instance or "").strip().lower() or _default_instance()

    mq = marqo.Client(url=args.marqo_url)
    index = mq.index(args.index)

    if not _index_has_instance_field(index):
        print(
            f"[abort] Index '{args.index}' has no filterable 'instance' field.\n"
            "        Structured Marqo indexes cannot gain a field in place — recreate the\n"
            "        index with the passage schema (now includes 'instance') and reingest\n"
            "        via scripts/bulk_reingest_sqlite_to_marqo.py instead.",
            file=sys.stderr,
        )
        return 2

    stats = index.get_stats()
    total = stats.get("numberOfDocuments") if isinstance(stats, dict) else None
    print(f"[info] Index '{args.index}' has instance field. Documents reported: {total}")

    offset = 0
    scanned = 0
    updated = 0
    while True:
        result = index.search(
            q="",
            limit=args.batch_size,
            offset=offset,
            attributes_to_retrieve=["instance"],
        )
        hits = result.get("hits", []) if isinstance(result, dict) else []
        if not hits:
            break

        batch = []
        for hit in hits:
            scanned += 1
            current = (hit.get("instance") or "").strip().lower()
            if args.only_missing and current:
                continue
            batch.append({"_id": hit["_id"], "instance": instance})

        if batch and not args.dry_run:
            index.update_documents(batch)
        updated += len(batch)
        offset += len(hits)
        if len(hits) < args.batch_size:
            break

    verb = "would update" if args.dry_run else "updated"
    print(f"[done] scanned={scanned} {verb}={updated} instance='{instance}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
