#!/usr/bin/env python3
"""
Inspect Marqo index fields (schema).

Prints allFields from the configured index so you can verify which fields
are available for search, filters, and scoring.
"""

import os
import sys
from pathlib import Path

# Use project venv/site-packages and local code
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import marqo  # type: ignore[import-untyped]


def main() -> None:
    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    index_name = os.environ.get("MARQO_INDEX", "documents-index")

    print(f"Connecting to Marqo at {marqo_url}")
    mq = marqo.Client(url=marqo_url)

    try:
        index = mq.index(index_name)
        settings = index.get_settings()
    except Exception as e:
        print(f"Error fetching settings for index '{index_name}': {e}")
        return

    all_fields = settings.get("allFields", [])
    tensor_fields = settings.get("tensorFields", [])

    print(f"\nIndex: {index_name}")
    print(f"Model: {settings.get('model')}")
    print(f"Type:  {settings.get('type')}")
    print(f"Tensor fields (vectorized): {tensor_fields}")
    print(f"\nFields ({len(all_fields)}):")

    for f in all_fields:
        name = f.get("name")
        f_type = f.get("type")
        features = f.get("features", [])
        print(f"  - {name}: type={f_type}, features={features}")


if __name__ == "__main__":
    main()
