#!/usr/bin/env python3
"""Start OCR-only workflows for files in a directory, skipping docs with OCR state."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx",
    ".xls", ".xlsx", ".csv",
    ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff",
}


def compute_fingerprint(path: Path) -> str:
    md5 = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()


def get_workflow_id(filepath: str) -> str:
    return f"doc-{hashlib.md5(filepath.encode()).hexdigest()[:12]}"


def already_has_ocr(conn: sqlite3.Connection, filepath: str, fingerprint: str) -> bool:
    workflow_id = get_workflow_id(filepath)
    row = conn.execute(
        """
        SELECT workflow_id, canonical_document_id, ocr_completed_at, stage
        FROM documents
        WHERE workflow_id = ?
           OR canonical_document_id = ?
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (workflow_id, fingerprint),
    ).fetchone()
    if not row:
        return False
    if row["ocr_completed_at"]:
        return True
    if row["stage"] in {"ocr_review", "translation_processing", "translation_review", "chunking",
                        "chunk_review", "ready_for_ingestion", "ingesting", "completed"}:
        return True
    page_row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM pages WHERE workflow_id = ?",
        (row["workflow_id"],),
    ).fetchone()
    return bool(page_row and page_row["cnt"] > 0)


def post_document(api_base: str, filepath: str) -> dict:
    query = urllib.parse.urlencode({"stop_after_ocr": "true"})
    url = f"{api_base.rstrip('/')}/documents?{query}"
    payload = json.dumps({"filepath": filepath}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", help="Directory containing source docs")
    parser.add_argument("--api-base", default="http://localhost:8001", help="Docs-pipeline API base URL")
    parser.add_argument("--db-path", default=os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db"))
    parser.add_argument("--limit", type=int, default=0, help="Optional max files to submit")
    args = parser.parse_args()

    directory = Path(args.directory)
    if not directory.exists():
        print(f"Directory not found: {directory}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row

    files = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS
    )
    if args.limit > 0:
        files = files[:args.limit]

    submitted = 0
    skipped = 0
    failed = 0

    for path in files:
        fingerprint = compute_fingerprint(path)
        if already_has_ocr(conn, str(path), fingerprint):
            skipped += 1
            print(f"SKIP {path.name}: OCR already present")
            continue
        try:
            result = post_document(args.api_base, str(path))
            submitted += 1
            print(f"SUBMIT {path.name}: {result.get('workflow_id')}")
        except urllib.error.HTTPError as exc:
            failed += 1
            body = exc.read().decode("utf-8", errors="replace")
            print(f"FAIL {path.name}: HTTP {exc.code} {body}")
        except Exception as exc:  # pragma: no cover - operational script
            failed += 1
            print(f"FAIL {path.name}: {exc}")

    print(json.dumps({
        "directory": str(directory),
        "candidate_files": len(files),
        "submitted": submitted,
        "skipped": skipped,
        "failed": failed,
    }, indent=2))
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
