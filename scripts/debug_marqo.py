#!/usr/bin/env python3
"""
Debug script to check Marqo index status and list all indexes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.vector_store import default_physical_index, get_vector_store  # noqa: E402


def main() -> int:
    store = get_vector_store()
    index_name = os.environ.get("MARQO_INDEX") or default_physical_index()

    print(f"Connecting to Marqo at {store.url}")

    print("\n=== All Marqo Indexes ===")
    try:
        indexes = store.list_indexes()
        print(f"Found {len(indexes)} indexes:")
        for idx in indexes:
            print(f"  - {idx}")
    except Exception as error:
        print(f"Error listing indexes: {error}")

    print(f"\n=== Checking index '{index_name}' ===")
    try:
        stats = store.get_stats(index_name)
        print(f"Stats: {stats}")

        settings = store.get_settings(index_name)
        print(f"\nIndex settings:")
        print(f"  Type: {settings.get('type')}")
        print(f"  Model: {settings.get('model')}")
        print(f"  Fields: {len(settings.get('allFields', []))}")
    except Exception as error:
        print(f"Index '{index_name}' does not exist or error: {error}")

    print(f"\n=== Searching index '{index_name}' ===")
    try:
        results = store.search(index_name, q="", limit=5)
        hits = results.get("hits", [])
        print(f"Found {len(hits)} documents in search results")
        if hits:
            print(f"Sample doc_id: {hits[0].get('doc_id', 'N/A')}")
    except Exception as error:
        print(f"Search error: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
