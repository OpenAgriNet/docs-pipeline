#!/usr/bin/env python3
"""Create Qdrant collection and print mapping (H100 helper)."""
from __future__ import annotations

import json
import os
import sys

INDEX = os.environ.get("QDRANT_INDEX_NAME", "amul-veterinary-rebuild-20260821-qdrant")
os.environ.setdefault("VECTOR_STORE_BACKEND", "qdrant")
os.environ.setdefault("QDRANT_URL", "http://qdrant:6333")
os.environ.setdefault("EMBEDDING_BACKEND", "fastembed")

from pipeline.vector_store import get_vector_store, passage_index_settings  # noqa: E402


def main() -> int:
    store = get_vector_store()
    if store.index_exists(INDEX):
        print("deleting", INDEX)
        store.delete_index(INDEX)
    print("creating", INDEX)
    settings = passage_index_settings()
    print("passage_index_settings:", json.dumps(settings, indent=2, default=str))
    store.create_index(INDEX, settings)
    print("stats:", store.get_stats(INDEX))
    print("field_names:", sorted(store.field_names(INDEX)))
    print("describe:", store.describe_index(INDEX))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
