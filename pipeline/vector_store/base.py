"""Vector store protocol shared by Marqo and Qdrant backends."""

from __future__ import annotations

from typing import Any, Optional, Protocol


class VectorStore(Protocol):
    backend: str

    def ensure_collection(self, name: str, recreate: bool = False) -> dict[str, Any]:
        ...

    def get_settings(self, name: str) -> dict[str, Any]:
        ...

    def get_stats(self, name: str) -> dict[str, Any]:
        ...

    def upsert(
        self,
        name: str,
        records: list[dict[str, Any]],
        batch_size: int = 32,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        # instance=None -> no tenant scoping (reserved for unrestricted /
        # maintenance callers); a concrete value scopes the write to that
        # tenant (stamps payload + guards against cross-tenant records).
        ...

    def delete_by_doc_id(
        self,
        name: str,
        doc_id: str,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        # instance=None -> no tenant filter; a concrete value restricts the
        # delete to records carrying that tenant.
        ...

    def delete_chunk(
        self,
        name: str,
        doc_id: str,
        chunk_num: int,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        # instance=None -> no tenant filter; a concrete value restricts the
        # delete to records carrying that tenant.
        ...

    def list_by_doc_id(
        self,
        name: str,
        doc_id: str,
        limit: int = 1000,
        *,
        instance: str | None = None,
    ) -> list[dict[str, Any]]:
        # instance=None -> no tenant filter; a concrete value restricts the
        # listing to records carrying that tenant.
        ...

    def get_document(self, name: str, point_id: str) -> dict[str, Any]:
        ...

    def search(
        self,
        name: str,
        query: str,
        limit: int = 12,
        search_mode: str = "TENSOR",
        exclude_reference: bool = True,
        domain_tags: Optional[list[str]] = None,
        use_e5_prefix: bool = True,
        hybrid_alpha: float = 0.6,
        ef_search: int = 256,
        attributes_to_retrieve: Optional[list[str]] = None,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        # instance=None -> no tenant filter (reserved for unrestricted /
        # maintenance callers); a concrete value restricts results to that
        # tenant via the mandatory `instance` payload filter.
        ...
