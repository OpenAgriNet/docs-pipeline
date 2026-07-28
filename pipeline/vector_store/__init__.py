"""Vector index backends — Qdrant is the default and only supported stack backend.

Marqo remains importable only when VECTOR_BACKEND=marqo for emergency rollback.
"""

from __future__ import annotations

import os

from .base import VectorStore


def get_vector_backend() -> str:
    """
    Resolve active vector backend.

    Preference:
      1. VECTOR_BACKEND env (qdrant|marqo)
      2. default qdrant (Marqo free tier is being removed; compose no longer ships Marqo)
    """
    explicit = (os.environ.get("VECTOR_BACKEND") or "").strip().lower()
    if explicit in {"qdrant", "marqo"}:
        return explicit
    return "qdrant"


def get_default_index_name() -> str:
    return (
        os.environ.get("QDRANT_COLLECTION_NAME")
        or os.environ.get("MARQO_INDEX_NAME")
        or "documents-index"
    )


def get_vector_store() -> VectorStore:
    backend = get_vector_backend()
    if backend == "marqo":
        from .marqo_store import MarqoVectorStore

        return MarqoVectorStore()
    from .qdrant_store import QdrantVectorStore

    return QdrantVectorStore()


__all__ = [
    "VectorStore",
    "get_vector_backend",
    "get_default_index_name",
    "get_vector_store",
]
