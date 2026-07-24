"""Qdrant vector store for docs-pipeline passage chunks."""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels
from qdrant_client.http.exceptions import UnexpectedResponse

from .embeddings import embed_passages, embed_query, get_vector_size

logger = logging.getLogger(__name__)

_client_cache: dict[tuple[str, Optional[str]], QdrantClient] = {}

PAYLOAD_FIELDS = (
    "doc_id",
    "workflow_id",
    "instance",
    "type",
    "source",
    "filename",
    "name_gu",
    "name_en",
    "title_en",
    "title_gu",
    "doc_language",
    "category_tags",
    "doc_short_description",
    "doc_llm_description",
    "ingestion_status",
    "description",
    "text",
    "chunk_num",
    "section",
    "token_count",
    "page_start",
    "page_end",
    "is_reference",
    "quality_score",
    "priority_rank",
    "domain_tags",
    "domain_tags_list",
    "text_for_embedding",
    "priority",
)


def _md5_to_uuid(hex_id: str) -> str:
    """Convert 32-char hex (or any hash) into a UUID-shaped point id."""
    h = re.sub(r"[^0-9a-fA-F]", "", hex_id or "")
    if len(h) < 32:
        h = (h + "00000000000000000000000000000000")[:32]
    else:
        h = h[:32]
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def point_id_for_record(record: dict[str, Any]) -> str:
    raw = str(record.get("_id") or "")
    if not raw:
        doc_id = str(record.get("doc_id") or "")
        chunk_num = record.get("chunk_num", 0)
        raw = f"{doc_id}:{chunk_num}"

    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        raw,
    ):
        return raw.lower()

    if re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        return _md5_to_uuid(raw)

    import hashlib

    return _md5_to_uuid(hashlib.md5(raw.encode()).hexdigest())


def _parse_qdrant_endpoint(raw_url: str) -> dict[str, Any]:
    """Normalize Qdrant URL for reverse-proxied HTTPS endpoints.

    qdrant-client defaults HTTPS without an explicit port to :6333, which breaks
    path-based proxies such as ``https://host/qdrant``. We split host + path
    prefix and force :443 for HTTPS when no port is given.
    """
    from urllib.parse import urlparse

    parsed = urlparse(raw_url.strip())
    if not parsed.scheme or not parsed.netloc:
        # Fall back to library defaults for bare hosts / legacy values.
        return {"url": raw_url.strip(), "prefix": None, "prefer_grpc": False}

    scheme = parsed.scheme.lower()
    host = parsed.hostname or parsed.netloc
    port = parsed.port
    path = (parsed.path or "").rstrip("/")

    if port is None:
        port = 443 if scheme == "https" else 6333

    base = f"{scheme}://{host}:{port}"
    prefix = path if path and path != "/" else None
    return {"url": base, "prefix": prefix, "prefer_grpc": False}


def get_qdrant_client(
    url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> QdrantClient:
    qdrant_url = (url or os.environ.get("QDRANT_URL") or "http://localhost:6333").strip()
    qdrant_key = api_key if api_key is not None else os.environ.get("QDRANT_API_KEY")
    cache_key = (qdrant_url, qdrant_key)
    if cache_key in _client_cache:
        return _client_cache[cache_key]

    is_local = any(host in qdrant_url for host in ("localhost", "127.0.0.1"))
    if not qdrant_key and not is_local:
        raise ValueError("QDRANT_API_KEY is required for remote Qdrant")

    timeout = float(os.environ.get("QDRANT_TIMEOUT_SECONDS", "60"))
    endpoint = _parse_qdrant_endpoint(qdrant_url)
    kwargs: dict[str, Any] = {
        "url": endpoint["url"],
        "timeout": timeout,
        "check_compatibility": False,
        "prefer_grpc": endpoint.get("prefer_grpc", False),
    }
    if endpoint.get("prefix"):
        kwargs["prefix"] = endpoint["prefix"]
    if qdrant_key:
        kwargs["api_key"] = qdrant_key

    client = QdrantClient(**kwargs)
    _client_cache[cache_key] = client
    return client


def _normalize_domain_tags(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        parts = [p.strip() for p in text.split("|") if p.strip()]
        return parts
    return []


def _record_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in PAYLOAD_FIELDS:
        if key in record and record[key] is not None:
            payload[key] = record[key]

    tags = _normalize_domain_tags(record.get("domain_tags_list") or record.get("domain_tags"))
    if tags:
        payload["domain_tags_list"] = tags
        payload["domain_tags"] = "|" + "|".join(tags) + "|"

    if "text" not in payload:
        payload["text"] = record.get("text") or ""
    return payload


def _hit_from_point(point: Any) -> dict[str, Any]:
    payload = dict(point.payload or {})
    hit = {
        "_id": str(point.id),
        "_score": float(getattr(point, "score", 0.0) or 0.0),
        **payload,
    }
    if hit.get("chunk_num") is not None:
        hit.setdefault("chunk_number", hit["chunk_num"])
    return hit


def _build_filter(
    doc_id: Optional[str] = None,
    chunk_num: Optional[int] = None,
    exclude_reference: bool = False,
    domain_tags: Optional[list[str]] = None,
    instance: str | None = None,
) -> Optional[qmodels.Filter]:
    must: list[qmodels.FieldCondition] = []
    if instance is not None:
        # Mandatory tenant scoping (defense-in-depth alongside per-tenant
        # collections). instance=None leaves behavior exactly as before.
        must.append(
            qmodels.FieldCondition(
                key="instance",
                match=qmodels.MatchValue(value=instance),
            )
        )
    if doc_id is not None:
        must.append(
            qmodels.FieldCondition(
                key="doc_id",
                match=qmodels.MatchValue(value=doc_id),
            )
        )
    if chunk_num is not None:
        must.append(
            qmodels.FieldCondition(
                key="chunk_num",
                match=qmodels.MatchValue(value=int(chunk_num)),
            )
        )
    if exclude_reference:
        must.append(
            qmodels.FieldCondition(
                key="is_reference",
                match=qmodels.MatchValue(value=False),
            )
        )
    for tag in domain_tags or []:
        tag = str(tag).strip().lower()
        if not tag:
            continue
        must.append(
            qmodels.FieldCondition(
                key="domain_tags_list",
                match=qmodels.MatchValue(value=tag),
            )
        )
    if not must:
        return None
    return qmodels.Filter(must=must)


class QdrantVectorStore:
    backend = "qdrant"

    def __init__(self, client: Optional[QdrantClient] = None):
        self.client = client or get_qdrant_client()

    def ensure_collection(self, name: str, recreate: bool = False) -> dict[str, Any]:
        vector_size = get_vector_size()
        exists = False
        try:
            self.client.get_collection(name)
            exists = True
        except Exception:
            exists = False

        if exists and not recreate:
            return {"index": name, "created": False, "backend": self.backend}

        if exists and recreate:
            self.client.delete_collection(name)

        self.client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=vector_size,
                distance=qmodels.Distance.COSINE,
            ),
        )

        payload_indexes = {
            "doc_id": qmodels.PayloadSchemaType.KEYWORD,
            "workflow_id": qmodels.PayloadSchemaType.KEYWORD,
            "filename": qmodels.PayloadSchemaType.KEYWORD,
            "instance": qmodels.PayloadSchemaType.KEYWORD,
            "chunk_num": qmodels.PayloadSchemaType.INTEGER,
            "is_reference": qmodels.PayloadSchemaType.BOOL,
            "domain_tags_list": qmodels.PayloadSchemaType.KEYWORD,
            "type": qmodels.PayloadSchemaType.KEYWORD,
            "source": qmodels.PayloadSchemaType.KEYWORD,
        }
        for field_name, schema in payload_indexes.items():
            try:
                self.client.create_payload_index(
                    collection_name=name,
                    field_name=field_name,
                    field_schema=schema,
                )
            except Exception as exc:
                logger.debug("Payload index %s skipped/exists: %s", field_name, exc)

        return {
            "index": name,
            "created": True,
            "backend": self.backend,
            "vector_size": vector_size,
            "distance": "Cosine",
            "message": "Qdrant collection created for passage embeddings",
        }

    def get_settings(self, name: str) -> dict[str, Any]:
        info = self.client.get_collection(name)
        vectors = info.config.params.vectors
        size = getattr(vectors, "size", None) if hasattr(vectors, "size") else None
        distance = str(getattr(vectors, "distance", "")) if vectors is not None else ""
        all_fields = [{"name": field} for field in PAYLOAD_FIELDS]
        return {
            "backend": self.backend,
            "collection": name,
            "vector_size": size,
            "distance": distance,
            "points_count": info.points_count,
            "indexed_vectors_count": getattr(info, "indexed_vectors_count", None),
            "status": str(getattr(info, "status", "")),
            "allFields": all_fields,
            "tensorFields": ["text_for_embedding"],
            "model": os.environ.get("EMBEDDING_MODEL", "intfloat/multilingual-e5-large"),
        }

    def get_stats(self, name: str) -> dict[str, Any]:
        info = self.client.get_collection(name)
        points = info.points_count
        return {
            "backend": self.backend,
            "numberOfDocuments": points,
            "points_count": points,
            "indexed_vectors_count": getattr(info, "indexed_vectors_count", None),
            "status": str(getattr(info, "status", "")),
        }

    def upsert(
        self,
        name: str,
        records: list[dict[str, Any]],
        batch_size: int = 32,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_collection(name, recreate=False)
        if not records:
            return {
                "records_ingested": 0,
                "index_stats": self.get_stats(name),
            }

        # Write guard: when a tenant scope is supplied, stamp it onto every
        # record and refuse any record that already carries a DIFFERENT
        # non-empty instance (prevents writing tenant-A chunks inside a call
        # scoped to tenant-B). instance=None leaves payloads untouched.
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

        passages: list[str] = []
        for record in records:
            text_for_emb = record.get("text_for_embedding")
            if text_for_emb is None:
                text_for_emb = record.get("text") or ""
            passages.append(str(text_for_emb))

        # If passages already include the E5 "passage:" prefix, do not double-prefix.
        use_prefix = not all(str(p or "").lower().startswith("passage:") for p in passages)
        vectors = embed_passages(passages, use_e5_prefix=use_prefix, batch_size=batch_size)

        points = [
            qmodels.PointStruct(
                id=point_id_for_record(record),
                vector=vector,
                payload=_record_payload(record),
            )
            for record, vector in zip(records, vectors)
        ]

        for i in range(0, len(points), batch_size):
            batch = points[i : i + batch_size]
            self.client.upsert(collection_name=name, points=batch)

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
        try:
            self.client.delete(
                collection_name=name,
                points_selector=qmodels.FilterSelector(
                    filter=_build_filter(doc_id=doc_id, instance=instance),
                ),
            )
            return {
                "deleted": -1,
                "doc_id": doc_id,
                "backend": self.backend,
                "mode": "filter",
            }
        except Exception as exc:
            return {
                "deleted": 0,
                "doc_id": doc_id,
                "error": str(exc),
                "backend": self.backend,
            }

    def delete_chunk(
        self,
        name: str,
        doc_id: str,
        chunk_num: int,
        *,
        instance: str | None = None,
    ) -> dict[str, Any]:
        try:
            filt = _build_filter(doc_id=doc_id, chunk_num=chunk_num, instance=instance)
            points, _ = self.client.scroll(
                collection_name=name,
                scroll_filter=filt,
                limit=10,
                with_payload=False,
                with_vectors=False,
            )
            if not points:
                return {"deleted": False, "reason": "not_found", "backend": self.backend}
            ids = [p.id for p in points]
            self.client.delete(
                collection_name=name,
                points_selector=qmodels.PointIdsList(points=ids),
            )
            return {
                "deleted": True,
                "chunk_id": str(ids[0]),
                "backend": self.backend,
            }
        except Exception as exc:
            return {"deleted": False, "error": str(exc), "backend": self.backend}

    def list_by_doc_id(
        self,
        name: str,
        doc_id: str,
        limit: int = 1000,
        *,
        instance: str | None = None,
    ) -> list[dict[str, Any]]:
        filt = _build_filter(doc_id=doc_id, instance=instance)
        hits: list[dict[str, Any]] = []
        next_offset = None
        remaining = limit
        while remaining > 0:
            page_limit = min(remaining, 256)
            points, next_offset = self.client.scroll(
                collection_name=name,
                scroll_filter=filt,
                limit=page_limit,
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                hits.append(_hit_from_point(point))
            remaining = limit - len(hits)
            if next_offset is None or not points:
                break
        return hits

    def get_document(self, name: str, point_id: str) -> dict[str, Any]:
        candidates = [point_id]
        if re.fullmatch(r"[0-9a-fA-F]{32}", point_id or ""):
            candidates.append(_md5_to_uuid(point_id))
        try:
            points = self.client.retrieve(
                collection_name=name,
                ids=candidates,
                with_payload=True,
                with_vectors=False,
            )
            if not points:
                raise KeyError(f"Point not found: {point_id}")
            return _hit_from_point(points[0])
        except Exception as exc:
            raise KeyError(f"Point not found: {point_id}") from exc

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
        mode = (search_mode or "TENSOR").upper()
        # Built once and reused by BOTH the LEXICAL scroll branch and the
        # TENSOR query_points/search branch, so the tenant filter applies to
        # every path. instance=None reproduces today's unfiltered behavior.
        query_filter = _build_filter(
            exclude_reference=exclude_reference,
            domain_tags=domain_tags,
            instance=instance,
        )

        if mode == "LEXICAL":
            tokens = [t.lower() for t in re.findall(r"[\w\-]+", query or "") if len(t) >= 2]
            page_limit = min(max(limit * 20, 50), 500)
            points, _ = self.client.scroll(
                collection_name=name,
                scroll_filter=query_filter,
                limit=page_limit,
                with_payload=True,
                with_vectors=False,
            )
            scored: list[tuple[float, Any]] = []
            for point in points:
                text = str((point.payload or {}).get("text") or "")
                desc = str((point.payload or {}).get("description") or "")
                blob = f"{text} {desc}".lower()
                if not tokens:
                    score = 0.0
                else:
                    score = float(sum(1.0 for tok in tokens if tok in blob))
                if score > 0:
                    scored.append((score, point))
            scored.sort(key=lambda item: item[0], reverse=True)
            hits = []
            for score, point in scored[:limit]:
                hit = _hit_from_point(point)
                hit["_score"] = score
                hits.append(hit)
            return {"hits": hits, "backend": self.backend, "search_mode": mode}

        vector = embed_query(query, use_e5_prefix=use_e5_prefix)
        search_params = qmodels.SearchParams(hnsw_ef=ef_search) if ef_search else None
        try:
            result = self.client.query_points(
                collection_name=name,
                query=vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                search_params=search_params,
            )
            points = result.points
        except UnexpectedResponse:
            points = self.client.search(
                collection_name=name,
                query_vector=vector,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                search_params=search_params,
            )

        hits = [_hit_from_point(point) for point in points]
        if attributes_to_retrieve:
            allowed = set(attributes_to_retrieve) | {"_id", "_score"}
            trimmed = []
            for hit in hits:
                trimmed.append({k: v for k, v in hit.items() if k in allowed})
            hits = trimmed
        return {"hits": hits, "backend": self.backend, "search_mode": mode}
