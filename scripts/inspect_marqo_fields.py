#!/usr/bin/env python3
"""
Inspect Marqo index fields (schema).

Prints allFields from the configured index so you can verify which fields
are available for search, filters, and scoring.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.vector_store import default_physical_index, get_vector_store  # noqa: E402


def main() -> None:
    store = get_vector_store()
    index_name = os.environ.get("MARQO_INDEX") or default_physical_index()

    print(f"Connecting to Marqo at {store.url}")
    try:
        settings = store.get_settings(index_name)
    except Exception as error:
        print(f"Error fetching settings for index '{index_name}': {error}")
        return

    all_fields = settings.get("allFields", [])
    tensor_fields = settings.get("tensorFields", [])

    print(f"\nIndex: {index_name}")
    print(f"Model: {settings.get('model')}")
    print(f"Type:  {settings.get('type')}")
    print(f"Tensor fields (vectorized): {tensor_fields}")
    print(f"\nFields ({len(all_fields)}):")

    for field in all_fields:
        name = field.get("name")
        f_type = field.get("type")
        features = field.get("features", [])
        print(f"  - {name}: type={f_type}, features={features}")


if __name__ == "__main__":
    main()
