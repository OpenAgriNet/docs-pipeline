#!/usr/bin/env python3
"""
Debug script to check Marqo index status and list all indexes.
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

print(f"Connecting to Marqo at {MARQO_URL}")
mq = marqo.Client(url=MARQO_URL)

# List all indexes
print("\n=== All Marqo Indexes ===")
try:
    indexes = mq.get_indexes()
    print(f"Found {len(indexes.get('results', []))} indexes:")
    for idx in indexes.get('results', []):
        print(f"  - {idx}")
except Exception as e:
    print(f"Error listing indexes: {e}")

# Check specific index
print(f"\n=== Checking index '{MARQO_INDEX}' ===")
try:
    index = mq.index(MARQO_INDEX)
    stats = index.get_stats()
    print(f"Stats: {stats}")
    
    # Try to get settings
    settings = index.get_settings()
    print(f"\nIndex settings:")
    print(f"  Type: {settings.get('type')}")
    print(f"  Model: {settings.get('model')}")
    print(f"  Fields: {len(settings.get('allFields', []))}")
except Exception as e:
    print(f"Index '{MARQO_INDEX}' does not exist or error: {e}")

# Try a search to see if there's any data
print(f"\n=== Searching index '{MARQO_INDEX}' ===")
try:
    index = mq.index(MARQO_INDEX)
    results = index.search(q="", limit=5)
    hits = results.get("hits", [])
    print(f"Found {len(hits)} documents in search results")
    if hits:
        print(f"Sample doc_id: {hits[0].get('doc_id', 'N/A')}")
except Exception as e:
    print(f"Search error: {e}")
