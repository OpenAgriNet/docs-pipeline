#!/usr/bin/env python3
"""
Count unique document IDs in Marqo index.
Does not disturb any running ingestion processes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.vector_store import default_physical_index, get_vector_store  # noqa: E402


def count_unique_doc_ids(
    marqo_url: str | None = None,
    index_name: str | None = None,
) -> int:
    """Count unique doc_ids in the Marqo index."""
    store = get_vector_store(url=marqo_url) if marqo_url else get_vector_store()
    index = index_name or os.environ.get("MARQO_INDEX") or default_physical_index()

    print(f"Connecting to Marqo at {store.url}")
    try:
        stats = store.get_stats(index)
        total_docs = stats.get("numberOfDocuments", 0)
        print(f"Total documents in index '{index}': {total_docs}")

        if total_docs == 0:
            print("No documents found in index")
            return 0

        seen_doc_ids: set[str] = set()
        offset = 0
        batch_size = 100

        while offset < total_docs:
            results = store.search(
                index,
                q="",
                limit=batch_size,
                offset=offset,
                attributes_to_retrieve=["doc_id"],
            )

            hits = results.get("hits", [])
            if not hits:
                break

            for hit in hits:
                doc_id = hit.get("doc_id")
                if doc_id:
                    seen_doc_ids.add(doc_id)

            offset += batch_size
            print(f"Processed {min(offset, total_docs)}/{total_docs} documents...", end="\r")

        print(f"\n\nUnique document IDs (doc_id): {len(seen_doc_ids)}")
        print(f"Total document chunks: {total_docs}")
        print(
            f"Average chunks per document: {total_docs / len(seen_doc_ids):.1f}"
            if seen_doc_ids
            else "N/A"
        )
        return len(seen_doc_ids)

    except Exception as error:
        print(f"Error: {error}")
        return 0


if __name__ == "__main__":
    count_unique_doc_ids()
