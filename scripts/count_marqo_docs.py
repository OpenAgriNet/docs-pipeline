#!/usr/bin/env python3
"""
Count unique document IDs in Marqo index.
Does not disturb any running ingestion processes.
"""

import os
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import marqo

MARQO_URL = os.environ.get("MARQO_URL", "http://localhost:8882")
MARQO_INDEX = os.environ.get("MARQO_INDEX", "documents-index")


def count_unique_doc_ids():
    """Count unique doc_ids in the Marqo index."""
    print(f"Connecting to Marqo at {MARQO_URL}")
    mq = marqo.Client(url=MARQO_URL)

    try:
        index = mq.index(MARQO_INDEX)
        stats = index.get_stats()
        total_docs = stats.get("numberOfDocuments", 0)
        print(f"Total documents in index '{MARQO_INDEX}': {total_docs}")

        if total_docs == 0:
            print("No documents found in index")
            return 0

        # Fetch all documents to get unique doc_ids
        # Use search with empty query and paginate through all results
        seen_doc_ids = set()
        offset = 0
        batch_size = 100

        while offset < total_docs:
            results = index.search(
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
        print(f"Average chunks per document: {total_docs / len(seen_doc_ids):.1f}" if seen_doc_ids else "N/A")

        return len(seen_doc_ids)

    except Exception as e:
        print(f"Error: {e}")
        return 0


if __name__ == "__main__":
    count_unique_doc_ids()
