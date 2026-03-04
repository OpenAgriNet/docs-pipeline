"""
Utility script to clear the Marqo index used by the pipeline.

This deletes the existing index (if it exists). The next ingestion run will
recreate the index using the schema defined in pipeline.activities.ingest_to_marqo.

Usage:
    python3 scripts/reset_marqo_index.py
"""

import os

import marqo


def main() -> None:
    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    index_name = os.environ.get("MARQO_INDEX_NAME", "documents-index")

    mq = marqo.Client(url=marqo_url)

    print(f"Connecting to Marqo at {marqo_url}")
    try:
        mq.index(index_name).get_stats()
    except Exception:
        print(f"Index '{index_name}' does not exist or is not reachable.")
        return

    confirm = input(f"Delete Marqo index '{index_name}'? This cannot be undone. [y/N]: ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    try:
        mq.delete_index(index_name)
        print(f"Deleted index '{index_name}'.")
    except Exception as e:
        print(f"Error deleting index: {e}")


if __name__ == "__main__":
    main()

