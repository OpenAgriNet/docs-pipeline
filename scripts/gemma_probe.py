#!/usr/bin/env python3
"""Probe Gemma vLLM translation endpoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("TRANSLATION_VLLM_BASE_URL", "http://localhost:8020/v1"),
    )
    parser.add_argument("--model", default=os.environ.get("TRANSLATION_MODEL", "gemma-4-31b-it"))
    parser.add_argument(
        "--text",
        default="ગુજરાતી ટેક્સ્ટ: દૂધના દર વિશે માહિતી.",
        help="Sample Gujarati text for a translation smoke test",
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    print(f"==> GET {base}/models")
    with urllib.request.urlopen(f"{base}/models", timeout=30) as resp:
        models = json.loads(resp.read())
    print(json.dumps(models, indent=2)[:2000])

    model_ids = [m.get("id") for m in models.get("data", []) if isinstance(m, dict)]
    model = args.model if args.model in model_ids else (model_ids[0] if model_ids else args.model)
    print(f"==> Using model: {model}")

    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Translate the following text to English. "
                        "Return only the translation.\n\n" + args.text
                    ),
                }
            ],
            "temperature": 0.0,
            "max_tokens": 256,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    print(f"==> POST {base}/chat/completions")
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
    content = result["choices"][0]["message"]["content"]
    print("Translation:")
    print(content)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
