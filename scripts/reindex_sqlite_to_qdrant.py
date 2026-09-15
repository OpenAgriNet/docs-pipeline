#!/usr/bin/env python3
"""Reindex SQLite chunks into a Qdrant collection (#151).

Does not touch Marqo. Set VECTOR_STORE_BACKEND=qdrant (and QDRANT_URL /
embedding env) before running.

Example (shadow collection, keep Marqo live)::

    set VECTOR_STORE_BACKEND=qdrant
    set QDRANT_URL=http://127.0.0.1:6333
    set EMBEDDING_BACKEND=fastembed
    python scripts/reindex_sqlite_to_qdrant.py \\
        --index-name amul-veterinary-rebuild-20260821-qdrant \\
        --recreate \\
        --batch-size 8
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index-name",
        default=os.environ.get("QDRANT_INDEX_NAME")
        or os.environ.get("MARQO_INDEX_NAME")
        or "documents-index-qdrant",
        help="Qdrant collection name (use a *-qdrant suffix while Marqo stays live)",
    )
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0, help="Max documents (0 = all)")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument(
        "--stage",
        default="completed",
        help="Document stage filter (default: completed). Empty string = all stages.",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Include soft-deleted documents",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Drop and recreate the collection before ingest",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count eligible chunks without writing",
    )
    args = parser.parse_args()

    os.environ.setdefault("VECTOR_STORE_BACKEND", "qdrant")
    if (os.environ.get("VECTOR_STORE_BACKEND") or "").strip().lower() not in {"qdrant", "qd"}:
        print(
            "ERROR: VECTOR_STORE_BACKEND must be 'qdrant' for this script "
            f"(got {os.environ.get('VECTOR_STORE_BACKEND')!r})",
            file=sys.stderr,
        )
        return 2

    # Import after env so factory picks Qdrant.
    from pipeline import db
    from pipeline.ingestion_records import prepare_records
    from pipeline.vector_store import get_vector_store, passage_index_settings

    db.init_db()
    store = get_vector_store()
    print(f"backend={os.environ.get('VECTOR_STORE_BACKEND')} url={store.url}")
    print(f"collection={args.index_name}")

    stage = args.stage.strip() or None
    page_size = 100
    offset = max(0, int(args.offset))
    max_docs = int(args.limit) if int(args.limit) > 0 else None

    docs: list[dict] = []
    while True:
        batch = db.list_documents(
            stage=stage,
            limit=page_size,
            offset=offset,
            include_demo=False,
            include_disabled=bool(args.include_disabled),
        )
        if not batch:
            break
        docs.extend(batch)
        offset += len(batch)
        if max_docs is not None and len(docs) >= max_docs:
            docs = docs[:max_docs]
            break
        if len(batch) < page_size:
            break

    print(f"documents={len(docs)} stage={stage!r}")

    total_chunks = 0
    for doc in docs:
        chunks = db.get_chunks(doc["workflow_id"], include_excluded=False)
        total_chunks += len(chunks)
    print(f"eligible_chunks={total_chunks}")
    if args.dry_run:
        return 0

    if args.recreate and store.index_exists(args.index_name):
        print(f"deleting collection {args.index_name}")
        store.delete_index(args.index_name)
    if not store.index_exists(args.index_name):
        print(f"creating collection {args.index_name}")
        store.create_index(args.index_name, passage_index_settings())

    upserted = 0
    errors = 0
    for i, doc in enumerate(docs, start=1):
        workflow_id = doc["workflow_id"]
        chunks = db.get_chunks(workflow_id, include_excluded=False)
        if not chunks:
            continue
        # Skip docs with queries disabled when the column exists.
        if doc.get("query_enabled") is not None and not bool(doc.get("query_enabled")):
            continue
        records = prepare_records(
            document_id=doc.get("document_id") or workflow_id,
            filename=doc.get("filename") or workflow_id,
            chunks=chunks,
            workflow_id=workflow_id,
            instance=doc.get("instance"),
            include_e5_prefix_field=True,
        )
        for record in records:
            if "query_enabled" not in record:
                record["query_enabled"] = True if doc.get("query_enabled") is None else bool(doc.get("query_enabled"))
        result = store.add_documents(
            args.index_name,
            records,
            batch_size=max(1, int(args.batch_size)),
        )
        upserted += len(records)
        errors += len(result.errors)
        if i % 10 == 0 or i == len(docs):
            print(
                f"[{i}/{len(docs)}] workflow={workflow_id} "
                f"chunks={len(records)} upserted_total={upserted} errors={errors}"
            )

    stats = store.get_stats(args.index_name)
    print(
        "done",
        {
            "documents": len(docs),
            "upserted_records": upserted,
            "errors": errors,
            "qdrant_points": stats.get("numberOfDocuments"),
        },
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
