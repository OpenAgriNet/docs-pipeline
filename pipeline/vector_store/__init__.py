"""Vector index backends (Qdrant primary, Marqo optional legacy)."""

from __future__ import annotations

import os
from typing import Protocol

from .base import VectorStore


def get_vector_backend() -> str:
    """
    Resolve active vector backend.

    Preference:
      1. VECTOR_BACKEND env (qdrant|marqo)
      2. QDRANT_URL set → qdrant
      3. fallback marqo
    """
    explicit = (os.environ.get("VECTOR_BACKEND") or "").strip().lower()
    if explicit in {"qdrant", "marqo"}:
        return explicit
    if (os.environ.get("QDRANT_URL") or "").strip():
        return "qdrant"
    return "marqo"


def get_default_index_name() -> str:
    if get_vector_backend() == "qdrant":
        return (
            os.environ.get("QDRANT_COLLECTION_NAME")
            or os.environ.get("MARQO_INDEX_NAME")
            or "documents-index"
        )
    return os.environ.get("MARQO_INDEX_NAME") or "documents-index"


def get_vector_store() -> VectorStore:
    backend = get_vector_backend()
    if backend == "qdrant":
        from .qdrant_store import QdrantVectorStore

        return QdrantVectorStore()
    from .marqo_store import MarqoVectorStore

    return MarqoVectorStore()


__all__ = [
    "VectorStore",
    "get_vector_backend",
    "get_default_index_name",
    "get_vector_store",
]
