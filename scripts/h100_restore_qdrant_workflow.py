#!/usr/bin/env python3
"""Restore one workflow into Qdrant from rebuild SQLite (ops recovery)."""
from __future__ import annotations

import os
import sys

WORKFLOW_ID = os.environ.get("RESTORE_WORKFLOW_ID", "rebuild-doc-a10082979490")
INDEX = os.environ.get("QDRANT_AB_INDEX", "amul-veterinary-rebuild-20260821-qdrant")


def main() -> int:
    os.environ.setdefault("VECTOR_STORE_BACKEND", "qdrant")
    os.environ.setdefault("EMBEDDING_BACKEND", "fastembed")
    os.environ.setdefault("EMBEDDING_NORMALIZE", "true")
    os.environ.setdefault("DOCUMENT_DB_PATH", "/data/rebuild-20260821/documents.db")

    from pipeline import db
    from pipeline.ingestion_records import prepare_records
    from pipeline.vector_store_qdrant import QdrantStore

    db.init_db()
    doc = db.get_document(WORKFLOW_ID)
    if not doc:
        # list search fallback
        docs = db.list_documents(stage="completed", limit=500, offset=0, include_demo=False, include_disabled=True)
        doc = next((d for d in docs if d.get("workflow_id") == WORKFLOW_ID), None)
    if not doc:
        print(f"document not found: {WORKFLOW_ID}")
        return 1
    chunks = db.get_chunks(WORKFLOW_ID, include_excluded=False)
    print(f"workflow={WORKFLOW_ID} chunks={len(chunks)}")
    records = prepare_records(
        document_id=doc.get("document_id") or WORKFLOW_ID,
        filename=doc.get("filename") or WORKFLOW_ID,
        chunks=chunks,
        workflow_id=WORKFLOW_ID,
        instance=doc.get("instance"),
        include_e5_prefix_field=True,
    )
    for record in records:
        record.setdefault("query_enabled", True)
    store = QdrantStore(url=os.environ.get("QDRANT_URL", "http://qdrant:6333"))
    result = store.add_documents(INDEX, records, batch_size=8)
    stats = store.get_stats(INDEX)
    print("done", {"upserted": len(records), "errors": len(result.errors), "points": stats.get("numberOfDocuments")})
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
