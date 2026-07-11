#!/usr/bin/env python3
"""Send a local PDF file to Chandra OCR (vLLM) and report success or failure."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CHANDRA_VLLM_BASE_URL", "http://localhost:8000/v1"),
        help="Chandra vLLM OpenAI-compatible base URL",
    )
    parser.add_argument(
        "--page-range",
        default="1-3",
        help='PDF page range, e.g. "1-3" or "1-5,7"',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pdf_path = Path(args.pdf_path)
    if not pdf_path.is_file():
        raise SystemExit(f"File not found: {pdf_path}")

    os.environ.setdefault("OCR_PROVIDER", "chandra")
    os.environ.setdefault("CHANDRA_VLLM_BASE_URL", args.base_url)

    try:
        from chandra.input import load_file
        from chandra.model import InferenceManager
        from chandra.model.schema import BatchInputItem
    except ImportError:
        raise SystemExit("Install chandra-ocr first: pip install chandra-ocr")

    endpoint = args.base_url.rstrip("/")
    if not endpoint.endswith("/v1"):
        endpoint = f"{endpoint}/v1"

    images = load_file(str(pdf_path), {"page_range": args.page_range})
    batch = [BatchInputItem(image=image, prompt_type="ocr_layout") for image in images]
    manager = InferenceManager(method="vllm")
    results = manager.generate(batch, vllm_api_base=endpoint)

    result = {
        "pdf_path": str(pdf_path),
        "base_url": endpoint,
        "page_range": args.page_range,
        "pages": len(results),
        "samples": [
            {
                "page": idx + 1,
                "markdown_chars": len((item.markdown or "").strip()),
                "markdown_sample": (item.markdown or "").strip()[:200],
                "error": bool(item.error),
            }
            for idx, item in enumerate(results[:3])
        ],
    }
    print(json.dumps(result, indent=2))
    return 1 if any(item.error for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
