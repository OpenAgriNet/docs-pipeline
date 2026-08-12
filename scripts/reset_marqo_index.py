"""
Utility script to clear the Marqo index used by the pipeline.

This deletes the existing index (if it exists). The next ingestion run will
recreate the index using the schema defined in pipeline.activities.ingest_to_marqo.

Defaults to dry-run. Pass ``--apply`` to delete.

Usage:
    python3 scripts/reset_marqo_index.py
    python3 scripts/reset_marqo_index.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.vector_store import default_physical_index, get_vector_store  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--marqo-url",
        default=os.environ.get("MARQO_URL", "http://localhost:8882"),
        help="Marqo URL (default: MARQO_URL or localhost:8882)",
    )
    parser.add_argument(
        "--index-name",
        default=os.environ.get("MARQO_INDEX_NAME") or default_physical_index(),
        help="Physical index to delete",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete the index (default is dry-run)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    store = get_vector_store(url=args.marqo_url)
    index_name = args.index_name

    print(f"Connecting to Marqo at {store.url}")
    if not store.index_exists(index_name):
        print(f"Index '{index_name}' does not exist or is not reachable.")
        return 0

    try:
        stats = store.get_stats(index_name)
    except Exception as error:
        print(f"Index '{index_name}' exists but stats failed: {error}")
        stats = {}

    if not args.apply:
        print(
            f"[dry-run] Would delete index '{index_name}' "
            f"(stats={stats}). Re-run with --apply to delete."
        )
        return 0

    try:
        store.delete_index(index_name)
        print(f"Deleted index '{index_name}'.")
        return 0
    except Exception as error:
        print(f"Error deleting index: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
