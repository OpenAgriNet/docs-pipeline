"""Logical index resolution and Marqo index operations."""

import logging
from typing import Optional

from fastapi import HTTPException

from .. import db, vector_store
from ..auth.models import AuthUser
from ..auth.tenancy import allowed_instances, default_instance, normalize_instance


def default_physical_index() -> str:
    return vector_store.default_physical_index()


def new_marqo_index_name(instance: str, name: str) -> str:
    """Return the canonical physical name for a newly provisioned index."""
    clean = (name or "").strip().lower()
    if not vector_store.is_valid_logical_index_name(clean):
        raise HTTPException(
            400,
            "index name must match ^[a-z0-9_]{1,40}$ (letters, digits, _ only)",
        )
    return vector_store.physical_index_name(normalize_instance(instance), clean)


def resolve_index(instance: str | None, name: Optional[str] = None) -> Optional[str]:
    """Resolve a tenant's logical index to a physical Marqo index."""
    normalized = normalize_instance(instance)
    physical = db.resolve_marqo_index(normalized, name)
    if physical:
        return physical
    if name:
        raise HTTPException(404, "Index not found")
    if normalized == default_instance():
        return default_physical_index()
    return None


class IndexSettingsView:
    """Expose a store/index pair through the capability-probe interface."""

    __slots__ = ("_store", "_index_name")

    def __init__(self, store: vector_store.VectorStore, index_name: str) -> None:
        self._store = store
        self._index_name = index_name

    def get_settings(self) -> dict:
        return self._store.get_settings(self._index_name)


_MARQO_INSTANCE_FILTER_SKIP_LOGGED = False


def marqo_instance_filter(user: AuthUser, index) -> Optional[str]:
    """Build a fail-closed tenant filter for a live Marqo index."""
    permitted = allowed_instances(user)
    if permitted is None:
        return None
    if not vector_store.index_has_instance_field(index):
        global _MARQO_INSTANCE_FILTER_SKIP_LOGGED
        if default_instance() in {str(item).strip().lower() for item in permitted}:
            return None
        if not _MARQO_INSTANCE_FILTER_SKIP_LOGGED:
            logging.debug(
                "Marqo index has no `instance` field; a non-default caller is "
                "failed closed (match nothing) on this legacy single-tenant index."
            )
            _MARQO_INSTANCE_FILTER_SKIP_LOGGED = True
        return vector_store.field_filter("doc_id", "__none__")
    if not permitted:
        return vector_store.field_filter("instance", "__none__")
    return vector_store.any_of_filter("instance", sorted(permitted))


def create_marqo_index_with_schema(
    marqo_index: str,
    embedding_model: Optional[str] = None,
    settings_override: Optional[dict] = None,
) -> dict:
    """Create a physical Marqo index with the canonical passage schema."""
    store = vector_store.get_vector_store()
    settings = vector_store.passage_index_settings(
        model=embedding_model,
        overrides=settings_override,
    )
    if store.index_exists(marqo_index):
        if db.get_index_by_marqo_index(marqo_index) is None:
            raise HTTPException(
                409,
                f"Physical Marqo index '{marqo_index}' already exists and is not "
                "registered to this tenant; refusing to adopt it.",
            )
        return settings
    store.create_index(marqo_index, settings)
    return settings


def delete_single_chunk_from_marqo(
    document_id: str,
    chunk_num: int,
    index_name: str = "documents-index",
    workflow_id: Optional[str] = None,
) -> dict:
    return vector_store.get_vector_store().delete_chunk(
        document_id,
        chunk_num,
        index_name,
        workflow_id=workflow_id,
    )


def delete_chunks_from_marqo(
    document_id: str,
    index_name: str = "documents-index",
    workflow_id: Optional[str] = None,
) -> dict:
    return vector_store.get_vector_store().delete_document(
        document_id,
        index_name,
        workflow_id=workflow_id,
    )
