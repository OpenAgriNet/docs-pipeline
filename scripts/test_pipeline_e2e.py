#!/usr/bin/env python3
"""End-to-end pipeline smoke test (run on H100 or via SSH tunnel to API)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _request(method: str, url: str, *, data=None, headers: dict | None = None, timeout: float = 30.0):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return resp.status, json.loads(body) if body else {}


def _get(url: str, timeout: float = 30.0) -> dict:
    status, payload = _request("GET", url, timeout=timeout)
    if status >= 400:
        raise RuntimeError(f"GET {url} -> HTTP {status}")
    return payload


def check_health(name: str, url: str) -> bool:
    try:
        payload = _get(url, timeout=10)
        print(f"  OK  {name}: {payload}")
        return True
    except Exception as exc:
        print(f"  FAIL {name}: {exc}")
        return False


def upload_pdf(api: str, pdf_path: Path, *, auto_approve: bool, stop_after_ocr: bool) -> str:
    import subprocess

    query = f"auto_approve={'true' if auto_approve else 'false'}&stop_after_ocr={'true' if stop_after_ocr else 'false'}"
    url = f"{api.rstrip('/')}/upload?{query}"
    result = subprocess.run(
        ["curl", "-sf", "-X", "POST", url, "-F", f"file=@{pdf_path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Upload failed: {result.stderr or result.stdout}")
    payload = json.loads(result.stdout)
    workflow_id = payload.get("workflow_id")
    if not workflow_id:
        raise RuntimeError(f"Upload response missing workflow_id: {payload}")
    print(f"  Uploaded {pdf_path.name} -> workflow_id={workflow_id}")
    return workflow_id


def poll_document(api: str, workflow_id: str, *, timeout_seconds: int, interval: float) -> dict:
    terminal = {
        "ocr_review",
        "translation_review",
        "chunk_review",
        "ready_for_ingestion",
        "ingesting",
        "completed",
        "failed",
    }
    deadline = time.time() + timeout_seconds
    last_stage = None
    while time.time() < deadline:
        doc = _get(f"{api.rstrip('/')}/documents/{workflow_id}")
        stage = doc.get("stage")
        if stage != last_stage:
            print(
                f"  stage={stage} pages={doc.get('page_count')} "
                f"chunks={doc.get('chunk_count')} error={doc.get('error_message')}"
            )
            last_stage = stage
        if stage in terminal:
            return doc
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for workflow {workflow_id} (last stage={last_stage})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=os.environ.get("PIPELINE_API", "http://127.0.0.1:8001"))
    parser.add_argument("--pdf", type=Path, default=Path("./sample.pdf"))
    parser.add_argument("--chandra-health", default=os.environ.get("CHANDRA_HEALTH_URL", "http://127.0.0.1:8010/health"))
    parser.add_argument("--gemma-health", default=os.environ.get("TRANSLATION_VLLM_BASE_URL", "http://localhost:8020/v1"))
    parser.add_argument("--mode", choices=["ocr", "full"], default="ocr", help="ocr=stop after OCR; full=entire pipeline")
    parser.add_argument("--timeout", type=int, default=1800, help="Max seconds to wait for workflow")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    args = parser.parse_args()

    print("==> Service health")
    ok_api = check_health("pipeline-api", f"{args.api.rstrip('/')}/health")
    ok_chandra = check_health("chandra-ocr", args.chandra_health)
    ok_gemma = check_health("gemma-translation", f"{args.gemma_health.rstrip('/')}/models")
    if not ok_api:
        print("ERROR: Pipeline API is not reachable")
        return 1
    if not ok_chandra:
        print("WARNING: Chandra OCR health check failed — OCR step will likely fail")
    if args.mode == "full" and not ok_gemma:
        print("WARNING: Gemma translation health check failed — translation step will likely fail")

    if args.mode == "full":
        translation_url = args.gemma_health.strip() or os.environ.get("TRANSLATION_VLLM_BASE_URL", "").strip()
        if not translation_url:
            print("ERROR: full pipeline requires TRANSLATION_VLLM_BASE_URL (Gemma endpoint not set)")
            return 1
        print(f"  translation endpoint: {translation_url}")

    if not args.pdf.is_file():
        print(f"ERROR: PDF not found: {args.pdf}")
        return 1

    stop_after_ocr = args.mode == "ocr"
    print(f"==> Upload ({'OCR only' if stop_after_ocr else 'full pipeline'})")
    workflow_id = upload_pdf(args.api, args.pdf, auto_approve=True, stop_after_ocr=stop_after_ocr)

    print("==> Polling workflow")
    doc = poll_document(args.api, workflow_id, timeout_seconds=args.timeout, interval=args.poll_interval)

    stage = doc.get("stage")
    if stage == "failed":
        print(f"FAILED: {doc.get('error_message')}")
        return 1

    expected = "ocr_review" if stop_after_ocr else "completed"
    if stage != expected:
        print(f"UNEXPECTED: expected stage={expected}, got {stage}")
        return 1

    pages = _get(f"{args.api.rstrip('/')}/documents/{workflow_id}/pages")
    print(f"==> Result: stage={stage} pages={len(pages)}")
    if pages:
        sample = pages[0]
        print(f"  page 1 lang={sample.get('detected_language')} chars={len(sample.get('original_markdown') or '')}")

    if not stop_after_ocr:
        chunks = _get(f"{args.api.rstrip('/')}/documents/{workflow_id}/chunks")
        translated = sum(1 for p in pages if p.get("translated_markdown"))
        print(f"  translated_pages={translated} chunks={len(chunks)}")

    print("PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as exc:
        print(f"ERROR: network: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
