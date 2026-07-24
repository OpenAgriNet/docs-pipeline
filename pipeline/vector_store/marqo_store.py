"""Legacy Marqo vector store wrapper (kept for VECTOR_BACKEND=marqo)."""

from __future__ import annotations

import os
from typing import Any, Optional


def _instance_clause(instance: str | None) -> Optional[str]:
    """Marqo filter clause scoping to a single tenant, or None for no filter.

    Mirrors the ``instance:(<value>)`` shape used by main's
    ``_marqo_instance_filter``. ``instance=None`` returns None (no scoping) so
    existing single-tenant callers are unaffected.
    """
    if instance is None:
        return None
    return f"instance:({instance})"


def _and_filters(*clauses: Optional[str]) -> Optional[str]:
    """AND together non-empty Marqo filter clauses (None-safe)."""
    parts = [c for c in clauses if c]
    if not parts:
        return None
    return " AND ".join(parts)


class MarqoVectorStore:
    backend = "marqo"

    def __init__(self, url: Optional[str] = None):
        import marqo

        self.url = url or os.environ.get("MARQO_URL", "http://localhost:8882")
        self.client = marqo.Client(url=self.url)

    def _index(self, name: str):
        return self.client.index(name)

    def ensure_collection(self, name: str, recreate: bool = False) -> dict[str, Any]:
        return {"index": name, "created": False, "backend": self.backend}

    def get_settings(self, name: str) -> dict[str, Any]:
        settings = self._index(name).get_settings()
        if isinstance(settings, dict):
            settings = {**settings, "backend": self.backend}
        return settings

    def get_stats(self, name: str) -> dict[str, Any]:
        stats = self._index(name).get_stats()
        if isinstance(stats, dict):
            stats = {**stats, "backend": self.backend}
        return stats

    def upsert(
        self,
        name: str,
        records: list[dict[str, Any]],
        batch_size: int = 32,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        # Write guard mirrors the Qdrant backend: when a tenant scope is
        # supplied, stamp it onto every record and refuse any record already
        # carrying a DIFFERENT non-empty instance. instance=None leaves
        # records untouched (today's behavior).
        if instance is not None:
            for record in records:
                existing = record.get("instance")
                if existing not in (None, "") and str(existing) != str(instance):
                    raise ValueError(
                        "Refusing to upsert record with instance "
                        f"{existing!r} into tenant-scoped write for "
                        f"{instance!r} (doc_id={record.get('doc_id')!r})"
                    )
                record["instance"] = instance
        index = self._index(name)
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            index.add_documents(batch)
        stats = self.get_stats(name)
        return {
            "records_ingested": len(records),
            "index_stats": stats,
            "supports_prefixed_tensor_field": True,
            "backend": self.backend,
        }

    def delete_by_doc_id(
        self,
        name: str,
        doc_id: str,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        index = self._index(name)
        results = index.search(
            q="",
            filter_string=_and_filters(f"doc_id:{doc_id}", _instance_clause(instance)),
            limit=1000,
            attributes_to_retrieve=["_id"],
        )
        ids = [hit["_id"] for hit in results.get("hits") or []]
        if ids:
            index.delete_documents(ids=ids)
        return {"deleted": len(ids), "doc_id": doc_id, "backend": self.backend}

    def delete_chunk(
        self,
        name: str,
        doc_id: str,
        chunk_num: int,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        index = self._index(name)
        results = index.search(
            q="",
            filter_string=_and_filters(
                f"doc_id:{doc_id} AND chunk_num:{chunk_num}",
                _instance_clause(instance),
            ),
            limit=1,
            attributes_to_retrieve=["_id"],
        )
        hits = results.get("hits") or []
        if not hits:
            return {"deleted": False, "reason": "not_found", "backend": self.backend}
        chunk_id = hits[0]["_id"]
        index.delete_documents(ids=[chunk_id])
        return {"deleted": True, "chunk_id": chunk_id, "backend": self.backend}

    def list_by_doc_id(
        self,
        name: str,
        doc_id: str,
        limit: int = 1000,
        *,
        instance: str | None = None,
    ) -> list[dict[str, Any]]:
        result = self._index(name).search(
            q="",
            filter_string=_and_filters(f"doc_id:{doc_id}", _instance_clause(instance)),
            limit=limit,
            attributes_to_retrieve=[
                "doc_id",
                "filename",
                "text",
                "chunk_num",
                "page_start",
                "page_end",
                "token_count",
                "is_reference",
            ],
        )
        return result.get("hits") or []

    def get_document(self, name: str, point_id: str) -> dict[str, Any]:
        return self._index(name).get_document(point_id)

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
        index = self._index(name)
        request: dict[str, Any] = {
            "q": query,
            "limit": limit,
            "search_method": (search_mode or "TENSOR").lower(),
            "ef_search": ef_search,
        }
        # instance=None -> unchanged filter_string (only the legacy
        # is_reference clause, if any); a concrete value ANDs in the tenant
        # scope alongside it.
        filter_string = _and_filters(
            "is_reference:false" if exclude_reference else None,
            _instance_clause(instance),
        )
        if filter_string:
            request["filter_string"] = filter_string
        if attributes_to_retrieve:
            request["attributes_to_retrieve"] = attributes_to_retrieve
        if (search_mode or "").upper() == "HYBRID":
            request["hybrid_parameters"] = {
                "alpha": hybrid_alpha,
                "rankingMethod": "rrf",
                "searchableAttributesLexical": ["text", "description"],
                "searchableAttributesTensor": ["text_for_embedding"],
            }
        result = index.search(**request)
        return {
            "hits": result.get("hits") or [],
            "backend": self.backend,
            "search_mode": (search_mode or "TENSOR").upper(),
        }
