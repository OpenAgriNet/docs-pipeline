"""Index (or remove) one advisory ``webhook_records`` row in a vector store collection."""

from __future__ import annotations

import logging
import os
from typing import Callable, Literal, Optional

from . import db
from .advisory_chunks import build_chunks

logger = logging.getLogger(__name__)


def _delete_points(store, collection: str, doc_id: str) -> None:
    """Delete a record's points; a missing collection is fine, any other failure is not."""
    result = store.delete_by_doc_id(collection, doc_id) or {}
    error = str(result.get("error") or "")
    if error and "doesn't exist" not in error and "not found" not in error.lower():
        raise RuntimeError(f"Could not delete points for {doc_id} from {collection}: {error}")


def index_advisory_record(
    record_id: str,
    store,
    collection: str,
    count_tokens: Callable[[str], int],
    mark: Literal["indexed", "promoted"] = "indexed",
    also_delete_from: Optional[tuple[object, str]] = None,
) -> dict:
    """Replace a record's points in ``collection`` with its current chunks.

    Order: read row, delete the record's old points, write the new chunks, then store the
    row's payload_hash (``indexed_hash`` or ``promoted_hash``). An unpublished record (no
    chunks) only gets its points deleted; ``also_delete_from=(store, collection)`` removes
    them from a second collection too (deletion only, never a content push).
    """
    row = db.get_webhook_record(record_id)
    if row is None:
        return {"record_id": record_id, "status": "missing", "chunks": 0}

    doc_id = str(row["id"])
    chunks = build_chunks(row, count_tokens)
    _delete_points(store, collection, doc_id)
    if chunks:
        store.upsert(collection, chunks, batch_size=8)
    elif also_delete_from is not None:
        other_store, other_collection = also_delete_from
        _delete_points(other_store, other_collection, doc_id)

    marker = db.mark_webhook_record_promoted if mark == "promoted" else db.mark_webhook_record_indexed
    marker(record_id, row["payload_hash"])
    logger.info("advisory %s record=%s collection=%s chunks=%s", mark, record_id, collection, len(chunks))
    return {"record_id": record_id, "status": "indexed" if chunks else "deleted", "chunks": len(chunks)}


def prod_target() -> Optional[tuple[object, str]]:
    """The PROD (store, collection) pair from the PROD_QDRANT_* env, or None when not configured."""
    url = (os.environ.get("PROD_QDRANT_URL") or "").strip()
    if not url:
        return None
    from .vector_store.qdrant_store import QdrantVectorStore, get_qdrant_client

    api_key = (os.environ.get("PROD_QDRANT_API_KEY") or "").strip() or None
    collection = (
        os.environ.get("PROD_QDRANT_COLLECTION_NAME")
        or os.environ.get("QDRANT_COLLECTION_NAME")
        or "documents-index"
    ).strip()
    return QdrantVectorStore(client=get_qdrant_client(url=url, api_key=api_key)), collection


def log_advisory_audit(record_id: str, mark: str, collection: str, result: dict) -> None:
    """Write one audit_logs row for an indexed / deleted / promoted advisory record."""
    action = "advisory_promoted" if mark == "promoted" else (
        "advisory_deleted" if result.get("status") == "deleted" else "advisory_indexed"
    )
    db.log_audit(
        workflow_id=f"advisory-{record_id}",
        document_id=record_id,
        action_type=action,
        entity_type="webhook_record",
        metadata={"collection": collection, "chunks": result.get("chunks", 0), "status": result.get("status")},
    )
