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
      an in-place ``update_documents`` of only ``instance`` will be rejected
      and you must instead recreate the index with the passage schema (which now
      includes ``instance``) and reingest. The old bulk-reingest script was
      removed (it restamped every tenant as the default instance and deleted the
      index by default); recreate with ``scripts/create_marqo_passage_index.sh``
      and reingest per document via ``POST /documents/{workflow_id}/reingest``.
      For unstructured indexes an
      in-place update may work. Verify your index type first with
      ``GET /admin/index/schema``.
    - Always take a backup / confirm you can reingest before mutating a live index.

Defaults to dry-run. Pass ``--apply`` to write.

Usage:
    python3 scripts/backfill_marqo_instance.py --index documents-index --instance tenant-a
    python3 scripts/backfill_marqo_instance.py --index documents-index --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.vector_store import (  # noqa: E402
    VectorStoreError,
    default_physical_index,
    get_vector_store,
)


def _default_instance() -> str:
    return (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower() or "default"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marqo-url", default=os.environ.get("MARQO_URL", "http://localhost:8882"))
    parser.add_argument(
        "--index",
        default=os.environ.get("MARQO_INDEX_NAME") or default_physical_index(),
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
        "--apply",
        action="store_true",
        help="Write updates to Marqo (default is dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=argparse.SUPPRESS,  # kept so older invocations stay dry-run
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    instance = (args.instance or "").strip().lower() or _default_instance()
    apply = bool(args.apply) and not bool(args.dry_run)

    store = get_vector_store(url=args.marqo_url)

    try:
        field_names = store.field_names(args.index)
    except VectorStoreError as error:
        print(f"[abort] Unable to read schema for '{args.index}': {error}", file=sys.stderr)
        return 2

    if "instance" not in field_names:
        print(
            f"[abort] Index '{args.index}' has no filterable 'instance' field.\n"
            "        Structured Marqo indexes cannot gain a field in place — recreate the\n"
            "        index with the passage schema (now includes 'instance') via\n"
            "        scripts/create_marqo_passage_index.sh and reingest per document\n"
            "        via POST /documents/{workflow_id}/reingest instead.",
            file=sys.stderr,
        )
        return 2

    try:
        stats = store.get_stats(args.index)
    except VectorStoreError as error:
        print(f"[abort] Unable to read stats for '{args.index}': {error}", file=sys.stderr)
        return 2

    total = stats.get("numberOfDocuments") if isinstance(stats, dict) else None
    mode = "apply" if apply else "dry-run"
    print(f"[info] Index '{args.index}' has instance field. Documents reported: {total} ({mode})")

    offset = 0
    scanned = 0
    updated = 0
    while True:
        result = store.search(
            args.index,
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

        if batch and apply:
            store.update_documents(args.index, batch)
        updated += len(batch)
        offset += len(hits)
        if len(hits) < args.batch_size:
            break

    verb = "would update" if not apply else "updated"
    print(f"[done] scanned={scanned} {verb}={updated} instance='{instance}'")
    if not apply:
        print("[dry-run] Re-run with --apply to write updates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
