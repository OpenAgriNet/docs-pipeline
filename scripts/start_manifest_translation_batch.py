#!/usr/bin/env python3
"""Start translation-only workflows for manifest-backed Gujarati/mixed documents in OCR review."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from temporalio.client import Client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import db  # noqa: E402
from pipeline.workflows import TranslationOnlyWorkflow  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temporal-host", default=os.environ.get("TEMPORAL_HOST", "localhost:7233"))
    parser.add_argument("--task-queue", default=os.environ.get("TASK_QUEUE", "ocr-pipeline"))
    parser.add_argument("--db-path", default=os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--selection",
        choices=["manifest_gujarati", "all_ocr_review"],
        default="manifest_gujarati",
        help="Choose whether to start only manifest rows marked Gujarati or any authoritative OCR-review docs.",
    )
    parser.add_argument("--report-path", default="/tmp/start_manifest_translation_batch_report.json")
    return parser.parse_args()


def eligible_documents(db_path: str, selection: str, limit: int = 0) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if selection == "all_ocr_review":
        query = """
            SELECT d.workflow_id, d.document_id, d.filename, d.source_manifest_name, m.doc_language
            FROM documents d
            LEFT JOIN document_manifest_entries m
              ON m.manifest_filename = d.source_manifest_name
            WHERE d.source_manifest_name IS NOT NULL
              AND d.stage = 'ocr_review'
              AND d.translation_completed_at IS NULL
            ORDER BY d.updated_at DESC, d.workflow_id
        """
    else:
        query = """
            SELECT d.workflow_id, d.document_id, d.filename, d.source_manifest_name, m.doc_language
            FROM documents d
            LEFT JOIN document_manifest_entries m
              ON m.manifest_filename = d.source_manifest_name
            WHERE d.source_manifest_name IS NOT NULL
              AND d.stage = 'ocr_review'
              AND d.translation_completed_at IS NULL
              AND LOWER(COALESCE(m.doc_language, '')) LIKE '%gujarati%'
            ORDER BY d.updated_at DESC, d.workflow_id
        """
    rows = conn.execute(query).fetchall()
    docs = [dict(r) for r in rows]
    return docs[:limit] if limit > 0 else docs


async def main() -> int:
    args = parse_args()
    db.init_db()
    client = await Client.connect(args.temporal_host)

    docs = eligible_documents(args.db_path, args.selection, args.limit)
    report: dict[str, object] = {
        "started_at": datetime.utcnow().isoformat(),
        "temporal_host": args.temporal_host,
        "task_queue": args.task_queue,
        "selection": args.selection,
        "translation_provider": os.environ.get("TRANSLATION_PROVIDER", "gemma_vllm"),
        "translation_model": os.environ.get("TRANSLATION_MODEL", "gemma-4"),
        "eligible_documents": len(docs),
        "submitted": 0,
        "skipped": 0,
        "failed": [],
    }

    for doc in docs:
        temporal_workflow_id = f"{doc['workflow_id']}-translation-{int(time.time())}"
        try:
            await client.start_workflow(
                TranslationOnlyWorkflow.run,
                args=[doc["workflow_id"], doc["document_id"], doc["filename"]],
                id=temporal_workflow_id,
                task_queue=args.task_queue,
            )
            job_id = db.create_document_job(
                workflow_id=doc["workflow_id"],
                job_type="translation_only",
                temporal_workflow_id=temporal_workflow_id,
                status="running",
                current_stage="translation_processing",
                config={
                    "translation_provider": os.environ.get("TRANSLATION_PROVIDER", "gemma_vllm"),
                    "translation_model": os.environ.get("TRANSLATION_MODEL", "gemma-4"),
                    "source": "manifest_ocr_review",
                },
            )
            db.update_document_fields(doc["workflow_id"], latest_job_id=job_id)
            report["submitted"] = int(report["submitted"]) + 1
        except Exception as exc:
            report["failed"].append(
                {
                    "workflow_id": doc["workflow_id"],
                    "filename": doc["filename"],
                    "error": str(exc),
                }
            )

    report["finished_at"] = datetime.utcnow().isoformat()
    Path(args.report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(__import__("asyncio").run(main()))
