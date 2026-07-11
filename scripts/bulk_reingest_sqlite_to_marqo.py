#!/usr/bin/env python3
"""
Recreate the Marqo passage index and bulk reingest records from SQLite chunks.

This is the canonical recovery path when the live Marqo index is missing or when
the current index shape must be replaced with the latest passage schema.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import marqo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pipeline import db  # noqa: E402
from pipeline.activities import _marqo_settings, _prepare_records  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marqo-url", default=os.environ.get("MARQO_URL", "http://localhost:8882"))
    parser.add_argument("--index-name", default=os.environ.get("MARQO_INDEX_NAME", "documents-index"))
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--document-limit", type=int, default=0, help="0 means no limit")
    parser.add_argument("--include-disabled", action="store_true")
    parser.add_argument("--include-demo", action="store_true")
    parser.add_argument("--skip-recreate", action="store_true", help="Reuse an existing index if present")
    parser.add_argument("--report-path", default="", help="Optional JSON report output path")
    return parser.parse_args()


def recreate_index(mq: marqo.Client, index_name: str, skip_recreate: bool) -> None:
    settings = _marqo_settings(use_tensor_prefix_field=True)
    if not skip_recreate:
        try:
            mq.delete_index(index_name)
        except Exception:
            pass
        mq.create_index(index_name, settings_dict=settings)
        return

    try:
        mq.get_index(index_name)
    except Exception:
        mq.create_index(index_name, settings_dict=settings)


def main() -> int:
    args = parse_args()
    db.init_db()

    mq = marqo.Client(url=args.marqo_url)
    recreate_index(mq, args.index_name, args.skip_recreate)
    index = mq.index(args.index_name)

    all_docs = db.list_documents(
        limit=1000000,
        offset=0,
        include_demo=args.include_demo,
        include_disabled=args.include_disabled,
    )
    if args.document_limit > 0:
        all_docs = all_docs[: args.document_limit]

    report: dict[str, object] = {
        "started_at": datetime.utcnow().isoformat(),
        "marqo_url": args.marqo_url,
        "index_name": args.index_name,
        "documents_seen": len(all_docs),
        "documents_indexed": 0,
        "documents_skipped_no_chunks": 0,
        "records_ingested": 0,
        "errors": [],
    }

    for doc in all_docs:
        workflow_id = doc["workflow_id"]
        document_id = doc["document_id"]
        filename = doc["filename"]
        chunks = db.get_chunks(workflow_id, include_excluded=False)
        if not chunks:
            report["documents_skipped_no_chunks"] = int(report["documents_skipped_no_chunks"]) + 1
            continue

        records = _prepare_records(document_id, filename, chunks, workflow_id=workflow_id)
        try:
            for start in range(0, len(records), args.batch_size):
                batch = records[start : start + args.batch_size]
                result = index.add_documents(batch)
                if result.get("errors"):
                    raise RuntimeError(json.dumps(result))

            db.upsert_document_index_status(
                workflow_id=workflow_id,
                index_name=args.index_name,
                marqo_doc_id=document_id,
                chunk_count_indexed=len(records),
                last_indexed_at=datetime.utcnow().isoformat(),
                last_verified_at=datetime.utcnow().isoformat(),
                schema_version="passage-v1",
                status="indexed",
                details={"records_ingested": len(records)},
            )
            report["documents_indexed"] = int(report["documents_indexed"]) + 1
            report["records_ingested"] = int(report["records_ingested"]) + len(records)
        except Exception as exc:
            report["errors"].append(
                {
                    "workflow_id": workflow_id,
                    "document_id": document_id,
                    "filename": filename,
                    "error": str(exc),
                }
            )

    report["finished_at"] = datetime.utcnow().isoformat()
    try:
        report["index_stats"] = index.get_stats()
    except Exception as exc:
        report["index_stats_error"] = str(exc)

    if args.report_path:
        Path(args.report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
