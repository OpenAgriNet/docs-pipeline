"""Qdrant-backed :class:`~pipeline.vector_store.VectorStore`.

Translates the Marqo-shaped kwargs used by ``run_search`` / ingest into Qdrant
named-vector queries (dense E5 + sparse BM25). Filter strings stay in the
existing ``field:value`` grammar and are parsed here only.
"""

from __future__ import annotations

import copy
import os
import re
import uuid
from typing import Any, Callable, Optional, Sequence

from .embedding import Embedder, SparseVector, ensure_passage_prefix, get_embedder, strip_e5_prefix
from .vector_store import (
    AddResult,
    IndexSchemaReport,
    VectorStoreError,
    core_passage_schema_field_names,
    get_marqo_doc_id,
    passage_index_settings,
    tags_from_marqo_field,
)

# Stable namespace so Marqo md5 hex ids map 1:1 to Qdrant UUID point ids.
_POINT_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # URL namespace

_DENSE_VECTOR = "dense"
_SPARSE_VECTOR = "bm25"
_DEFAULT_QDRANT_URL = "http://localhost:6333"
_SENTENCE_WINDOW_SIZE = 3
_SENTENCE_WINDOW_OVERLAP = 1

# Qdrant-side companion to the flat ``domain_tags`` string. Marqo has no
# list-membership filter, so tags are stored pipe-wrapped and matched as a
# substring; Qdrant's text index tokenises on words and drops the pipes, which
# makes ``|species:cattle|`` match ``|species:cattle-breed|``. Tag keys
# therefore get their own keyword array, matched exactly.
_DOMAIN_TAG_KEYS_FIELD = "domain_tags_keys"

# Payload keys that are indexed for filter (mirrors Marqo filterable fields we use).
_PAYLOAD_INDEXES: tuple[tuple[str, str], ...] = (
    ("record_id", "keyword"),
    ("doc_id", "keyword"),
    ("workflow_id", "keyword"),
    ("instance", "keyword"),
    ("filename", "keyword"),
    ("is_reference", "bool"),
    ("query_enabled", "bool"),
    ("chunk_num", "integer"),
    # ``domain_tags`` keeps the Marqo-shaped pipe-delimited string for display and
    # payload parity; filters run on the keyword array, which is the only form
    # Qdrant can match exactly (see _DOMAIN_TAG_KEYS_FIELD).
    ("domain_tags", "text"),
    (_DOMAIN_TAG_KEYS_FIELD, "keyword"),
    ("type", "keyword"),
    ("source", "keyword"),
    ("section", "keyword"),
    ("doc_language", "keyword"),
)

_BOOL_TRUE = {"true", "1", "yes", "on"}
_BOOL_FALSE = {"false", "0", "no", "off"}

# Marqo filter atoms we generate today.
_TERM_RE = re.compile(
    r"""
    (?P<field>[A-Za-z_][A-Za-z0-9_]*)
    :
    (?:
        \((?P<paren>(?:\\.|[^\\)])*)\)
        |
        (?P<bare>[^\s\)]+)
    )
    """,
    re.VERBOSE,
)


def qdrant_url() -> str:
    return (os.environ.get("QDRANT_URL") or _DEFAULT_QDRANT_URL).strip() or _DEFAULT_QDRANT_URL


def record_id_to_point_id(record_id: str) -> str:
    """Map Marqo ``_id`` (md5 hex) to a Qdrant UUID string."""
    return str(uuid.uuid5(_POINT_NAMESPACE, f"docs-pipeline:{record_id}"))


def sentence_windows(text: str) -> list[str]:
    """Split a passage into overlapping 3-sentence windows, as in Marqo's schema."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return [""]
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?।॥])\s+|\n+", cleaned)
        if part.strip()
    ]
    if not sentences:
        return [cleaned]
    step = _SENTENCE_WINDOW_SIZE - _SENTENCE_WINDOW_OVERLAP
    windows = []
    for start in range(0, len(sentences), step):
        windows.append(" ".join(sentences[start : start + _SENTENCE_WINDOW_SIZE]))
        if start + _SENTENCE_WINDOW_SIZE >= len(sentences):
            break
    return windows


def point_id_for_record(record: dict) -> str:
    record_id = str(record.get("_id") or record.get("record_id") or "").strip()
    if not record_id:
        raise VectorStoreError("record missing _id/record_id")
    return record_id_to_point_id(record_id)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _BOOL_TRUE:
        return True
    if text in _BOOL_FALSE:
        return False
    return bool(value)


def _unescape(value: str) -> str:
    return value.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")


def parse_filter_string(filter_string: str | None):
    """Parse Marqo-style filter strings into a Qdrant Filter (or None).

    Supports the clauses this pipeline emits: ``AND`` / ``OR``, ``field:value``,
    ``field:(value)``, and parenthesised OR groups from ``any_of_filter``.
    """
    from qdrant_client.http import models as rest

    text = (filter_string or "").strip()
    if not text:
        return None

    def _condition(field: str, raw: str):
        value = _unescape(raw)
        if field in {"is_reference", "query_enabled"}:
            return rest.FieldCondition(key=field, match=rest.MatchValue(value=_as_bool(value)))
        if field in {"chunk_num", "token_count", "page_start", "page_end"}:
            try:
                return rest.FieldCondition(key=field, match=rest.MatchValue(value=int(value)))
            except ValueError:
                return rest.FieldCondition(key=field, match=rest.MatchValue(value=value))
        if field == "domain_tags":
            keys = tags_from_marqo_field(value)
            if not keys:
                # An empty tag clause must match nothing rather than everything.
                return rest.FieldCondition(
                    key=_DOMAIN_TAG_KEYS_FIELD, match=rest.MatchValue(value="__none__")
                )
            conditions = [
                rest.FieldCondition(key=_DOMAIN_TAG_KEYS_FIELD, match=rest.MatchValue(value=key))
                for key in keys
            ]
            if len(conditions) == 1:
                return conditions[0]
            return rest.Filter(must=conditions)
        return rest.FieldCondition(key=field, match=rest.MatchValue(value=value))

    def _parse_and_expr(expr: str):
        parts = [part.strip() for part in re.split(r"\s+AND\s+", expr, flags=re.IGNORECASE) if part.strip()]
        must = []
        for part in parts:
            must.append(_parse_atom(part))
        if len(must) == 1:
            return must[0]
        return rest.Filter(must=must)

    def _parse_atom(atom: str):
        atom = atom.strip()
        if atom.startswith("(") and atom.endswith(")"):
            inner = atom[1:-1].strip()
            if re.search(r"\s+OR\s+", inner, flags=re.IGNORECASE):
                alts = [
                    part.strip()
                    for part in re.split(r"\s+OR\s+", inner, flags=re.IGNORECASE)
                    if part.strip()
                ]
                return rest.Filter(should=[_parse_atom(alt) for alt in alts])
            return _parse_and_expr(inner)
        match = _TERM_RE.fullmatch(atom)
        if not match:
            raise VectorStoreError(f"Unsupported filter clause: {atom!r}")
        field = match.group("field")
        raw = match.group("paren") if match.group("paren") is not None else match.group("bare")
        return _condition(field, raw)

    try:
        parsed = _parse_and_expr(text)
    except VectorStoreError:
        raise
    except Exception as error:
        raise VectorStoreError(f"Unable to parse filter_string={text!r}: {error}") from error

    if isinstance(parsed, rest.Filter):
        return parsed
    return rest.Filter(must=[parsed])


def _with_domain_tag_keys(payload: dict[str, Any]) -> dict[str, Any]:
    """Derive the filterable tag keyword array from the flat ``domain_tags`` field.

    Kept next to every write path: the display string and the array it is filtered
    through are two halves of one encoding, and a payload patch that updated only
    one of them would make tag filters disagree with the stored tags.
    """
    if "domain_tags" not in payload:
        return payload
    payload[_DOMAIN_TAG_KEYS_FIELD] = tags_from_marqo_field(payload.get("domain_tags"))
    return payload


def _payload_from_record(record: dict) -> dict[str, Any]:
    record_id = str(record.get("_id") or record.get("record_id") or "").strip()
    payload: dict[str, Any] = {}
    for key, value in record.items():
        if key in {"_id"}:
            continue
        payload[key] = value
    payload["record_id"] = record_id
    _with_domain_tag_keys(payload)
    if "query_enabled" not in payload or payload.get("query_enabled") is None:
        payload["query_enabled"] = True
    else:
        payload["query_enabled"] = _as_bool(payload["query_enabled"])
    if "is_reference" in payload and payload.get("is_reference") is not None:
        payload["is_reference"] = _as_bool(payload["is_reference"])
    else:
        payload["is_reference"] = False
    return payload


def _hit_from_point(point) -> dict[str, Any]:
    payload = dict(point.payload or {})
    # Qdrant-only filter helper; hits stay Marqo-shaped for the API/UI.
    payload.pop(_DOMAIN_TAG_KEYS_FIELD, None)
    record_id = payload.pop("record_id", None) or payload.get("_id")
    hit = dict(payload)
    hit["_id"] = record_id
    score = getattr(point, "score", None)
    if score is not None:
        hit["_score"] = float(score)
    return hit


def _sparse_to_qdrant(sparse: SparseVector):
    from qdrant_client.http import models as rest

    return rest.SparseVector(indices=list(sparse.indices), values=list(sparse.values))


def _sparse_modifier_name(sparse_cfg: Any) -> str | None:
    """Lower-cased modifier of a live sparse vector config (``"idf"`` or None).

    A collection created before the IDF change reports no modifier, which is the
    difference between a real BM25 lexical arm and raw term counts — so the name
    is read from the live config rather than assumed from our create call.
    """
    if sparse_cfg is None:
        return None
    if isinstance(sparse_cfg, dict):
        modifier = sparse_cfg.get("modifier")
    else:
        modifier = getattr(sparse_cfg, "modifier", None)
    modifier = getattr(modifier, "value", modifier)
    if modifier is None:
        return None
    return str(modifier).strip().lower() or None


def _fuse_hybrid_hits(
    dense_points: Sequence[Any],
    sparse_points: Sequence[Any],
    *,
    alpha: float,
    ranking_method: str,
    rrf_k: int,
    offset: int,
    limit: int,
) -> list[Any]:
    """Combine Qdrant dense and BM25 candidates using the Marqo search knobs.

    Marqo exposes ``alpha`` (semantic weight) and either RRF or normalized
    linear ranking. Qdrant's built-in RRF fusion cannot apply those weights, so
    fuse the bounded result lists here and retain the original point payloads.
    """
    alpha = min(1.0, max(0.0, float(alpha)))
    method = (ranking_method or "rrf").strip().lower()
    if method not in {"rrf", "normalize_linear"}:
        raise VectorStoreError(f"Unsupported hybrid ranking method: {ranking_method!r}")

    dense_scores = {str(point.id): float(point.score or 0.0) for point in dense_points}
    sparse_scores = {str(point.id): float(point.score or 0.0) for point in sparse_points}
    dense_ranks = {str(point.id): rank for rank, point in enumerate(dense_points, 1)}
    sparse_ranks = {str(point.id): rank for rank, point in enumerate(sparse_points, 1)}
    points_by_id = {str(point.id): point for point in [*dense_points, *sparse_points]}
    ids = set(points_by_id)

    if method == "rrf":
        k = max(1, int(rrf_k))
        scores = {
            point_id: (
                alpha / (k + dense_ranks[point_id]) if point_id in dense_ranks else 0.0
            )
            + (
                (1.0 - alpha) / (k + sparse_ranks[point_id])
                if point_id in sparse_ranks
                else 0.0
            )
            for point_id in ids
        }
    else:
        def normalize(values: dict[str, float]) -> dict[str, float]:
            if not values:
                return {}
            low, high = min(values.values()), max(values.values())
            if high <= low:
                return {point_id: 1.0 for point_id in values}
            return {point_id: (value - low) / (high - low) for point_id, value in values.items()}

        dense_norm = normalize(dense_scores)
        sparse_norm = normalize(sparse_scores)
        scores = {
            point_id: alpha * dense_norm.get(point_id, 0.0)
            + (1.0 - alpha) * sparse_norm.get(point_id, 0.0)
            for point_id in ids
        }

    ranked = sorted(ids, key=lambda point_id: (-scores[point_id], point_id))
    output = []
    for point_id in ranked[max(0, int(offset)) : max(0, int(offset)) + max(0, int(limit))]:
        point = points_by_id[point_id]
        # Return a shallow copy so the fused score never mutates SDK-owned data.
        fused_point = copy.copy(point)
        fused_point.score = scores[point_id]
        output.append(fused_point)
    return output


class QdrantStore:
    """:class:`VectorStore` backed by Qdrant + external E5/BM25 embeddings."""

    def __init__(
        self,
        *,
        url: Optional[str] = None,
        api_key: Optional[str] = None,
        embedder: Optional[Embedder] = None,
        client: Any = None,
    ) -> None:
        self._url_override = (url or "").strip() or None
        self._api_key = (api_key if api_key is not None else os.environ.get("QDRANT_API_KEY", "")).strip()
        self._embedder = embedder
        self._client = client

    @property
    def url(self) -> str:
        return self._url_override or qdrant_url()

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = get_embedder()
        return self._embedder

    def client(self):
        if self._client is not None:
            return self._client
        from qdrant_client import QdrantClient

        kwargs: dict[str, Any] = {"url": self.url, "check_compatibility": False}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        self._client = QdrantClient(**kwargs)
        return self._client

    # -- reads ---------------------------------------------------------------

    def search(self, index: str, **request: Any) -> dict:
        from qdrant_client.http import models as rest

        try:
            query = request.get("q") or ""
            limit = int(request.get("limit") or 10)
            offset = max(0, int(request.get("offset") or 0))
            method = str(request.get("search_method") or "hybrid").strip().lower()
            query_filter = parse_filter_string(request.get("filter_string"))
            hybrid = request.get("hybrid_parameters") or {}
            rrf_k = int(hybrid.get("rrfK") or hybrid.get("rrf_k") or 60)
            alpha = min(1.0, max(0.0, float(hybrid.get("alpha", 0.6))))
            ranking_method = str(
                hybrid.get("rankingMethod") or hybrid.get("ranking_method") or "rrf"
            )

            # Filter-only enumeration (search_all_hits / backfill) uses q="".
            # Qdrant needs scroll for that; numeric offset must still advance pages.
            query_text = str(query).strip()
            if not query_text or not strip_e5_prefix(query_text).strip():
                points = self._scroll_page(
                    index, query_filter=query_filter, limit=limit, offset=offset
                )
                return {"hits": [_hit_from_point(point) for point in points]}

            dense = None
            sparse = None
            if method not in {"lexical", "keyword"}:
                # MaxSim expects a query matrix, including for a single query.
                dense = [self.embedder.embed_queries([str(query)]).dense[0]]
            if method not in {"tensor", "tensors"}:
                sparse_text = strip_e5_prefix(str(query)) or str(query)
                sparse = _sparse_to_qdrant(self.embedder.embed_sparse_plain([sparse_text])[0])

            if method in {"tensor", "tensors"}:
                points = self.client().query_points(
                    collection_name=index,
                    query=dense,
                    using=_DENSE_VECTOR,
                    query_filter=query_filter,
                    limit=limit,
                    offset=offset,
                    with_payload=True,
                ).points
            elif method in {"lexical", "keyword"}:
                points = self.client().query_points(
                    collection_name=index,
                    query=sparse,
                    using=_SPARSE_VECTOR,
                    query_filter=query_filter,
                    limit=limit,
                    offset=offset,
                    with_payload=True,
                ).points
            else:
                # Query each arm separately so Marqo's semantic/lexical alpha
                # and rankingMethod settings can be applied to their candidates.
                prefetch_limit = max(limit + offset, rrf_k)
                dense_points = self.client().query_points(
                    collection_name=index,
                    query=dense,
                    using=_DENSE_VECTOR,
                    query_filter=query_filter,
                    limit=prefetch_limit,
                    with_payload=True,
                ).points
                sparse_points = self.client().query_points(
                    collection_name=index,
                    query=sparse,
                    using=_SPARSE_VECTOR,
                    query_filter=query_filter,
                    limit=prefetch_limit,
                    with_payload=True,
                ).points
                points = _fuse_hybrid_hits(
                    dense_points,
                    sparse_points,
                    alpha=alpha,
                    ranking_method=ranking_method,
                    rrf_k=rrf_k,
                    offset=offset,
                    limit=limit,
                )
            return {"hits": [_hit_from_point(point) for point in points]}
        except VectorStoreError:
            raise
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def _scroll_page(
        self,
        index: str,
        *,
        query_filter,
        limit: int,
        offset: int,
    ) -> list[Any]:
        """Return one page of filter matches, honoring numeric Marqo-style offset."""
        need = offset + limit
        collected: list[Any] = []
        next_offset = None
        while len(collected) < need:
            batch, next_offset = self.client().scroll(
                collection_name=index,
                scroll_filter=query_filter,
                limit=min(256, need - len(collected)),
                offset=next_offset,
                with_payload=True,
                with_vectors=False,
            )
            if not batch:
                break
            collected.extend(batch)
            if next_offset is None:
                break
        return collected[offset : offset + limit]

    def get_document(self, index: str, doc_id: str) -> dict:
        try:
            point_id = record_id_to_point_id(doc_id) if _looks_like_md5(doc_id) else doc_id
            points = self.client().retrieve(
                collection_name=index,
                ids=[point_id],
                with_payload=True,
                with_vectors=False,
            )
            if not points:
                raise VectorStoreError(f"Document '{doc_id}' not found in '{index}'")
            return _hit_from_point(points[0])
        except VectorStoreError:
            raise
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def _live_collection_info(self, index: str):
        try:
            return self.client().get_collection(index)
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def _live_vector_config(self, info) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return (dense_named, sparse_named) maps from a live collection info."""
        params = getattr(info, "config", None)
        params = getattr(params, "params", None) if params is not None else None
        vectors = getattr(params, "vectors", None) if params is not None else None
        sparse = getattr(params, "sparse_vectors", None) if params is not None else None

        dense_map: dict[str, Any] = {}
        if isinstance(vectors, dict):
            dense_map = dict(vectors)
        elif vectors is not None and not isinstance(vectors, dict):
            # Single unnamed vector config — not our named-vector layout.
            dense_map = {"": vectors}

        sparse_map: dict[str, Any] = {}
        if isinstance(sparse, dict):
            sparse_map = dict(sparse)
        elif sparse is not None:
            try:
                sparse_map = dict(sparse)
            except (TypeError, ValueError):
                sparse_map = {}
        return dense_map, sparse_map

    def _payload_field_sets(self, info) -> tuple[set[str], set[str]]:
        """Return (live payload_schema names, advertised Marqo-shaped names).

        ``allFields`` still unions a documented floor so ``project_records()``
        does not drop passage keys (Qdrant accepts undeclared payload; Marqo
        does not, and ingest still projects through that helper). ``missing_core``
        is computed from live ``payload_schema`` only — unioning the full
        canonical schema here is what made drift invisible. Tenant filters still
        fail closed: a ``MatchValue`` on an absent key matches nothing.
        """
        payload_schema = getattr(info, "payload_schema", None) or {}
        live = set(payload_schema.keys()) if isinstance(payload_schema, dict) else set()
        advertised = set(live)
        advertised.update(name for name, _ in _PAYLOAD_INDEXES)
        advertised.update(core_passage_schema_field_names())
        advertised.update({"text", "text_for_embedding", "description"})
        return live, advertised

    def get_settings(self, index: str) -> dict:
        if not self.index_exists(index):
            raise VectorStoreError(f"Index '{index}' not found")
        info = self._live_collection_info(index)
        dense_map, sparse_map = self._live_vector_config(info)

        dense_cfg = dense_map.get(_DENSE_VECTOR)
        dense_size = getattr(dense_cfg, "size", None) if dense_cfg is not None else None
        dense_distance = getattr(dense_cfg, "distance", None) if dense_cfg is not None else None
        if hasattr(dense_distance, "value"):
            dense_distance = dense_distance.value
        dense_multivector = (
            getattr(dense_cfg, "multivector_config", None)
            if dense_cfg is not None
            else None
        )
        dense_comparator = getattr(dense_multivector, "comparator", None)
        if hasattr(dense_comparator, "value"):
            dense_comparator = dense_comparator.value

        sparse_cfg = sparse_map.get(_SPARSE_VECTOR)
        sparse_modifier = _sparse_modifier_name(sparse_cfg)

        live_names, advertised_names = self._payload_field_sets(info)
        all_fields = [{"name": name, "type": "text"} for name in sorted(advertised_names)]
        settings = {
            "type": "structured",
            "backend": "qdrant",
            "allFields": all_fields,
            "tensorFields": [],
            "vectors": {
                name: {
                    "size": getattr(cfg, "size", None),
                    "distance": getattr(
                        getattr(cfg, "distance", None),
                        "value",
                        getattr(cfg, "distance", None),
                    ),
                    "multivector_comparator": getattr(
                        getattr(cfg, "multivector_config", None),
                        "comparator",
                        None,
                    ),
                }
                for name, cfg in dense_map.items()
            },
            "sparse_vectors": {
                name: {"modifier": _sparse_modifier_name(cfg)}
                for name, cfg in sparse_map.items()
            },
            "dense": {
                "name": _DENSE_VECTOR,
                "size": dense_size,
                "distance": dense_distance,
                "multivector_comparator": dense_comparator,
                "expected_size": int(self.embedder.dense_dim),
            },
            "sparse": {
                "name": _SPARSE_VECTOR,
                "present": sparse_cfg is not None,
                "modifier": sparse_modifier,
                "idf": sparse_modifier == "idf",
            },
            # Live Qdrant payload indexes only. Used by describe_index missing_core
            # so a synthetic allFields floor cannot hide schema drift.
            "payload_schema_fields": sorted(live_names),
        }
        # Same gate as describe_index: a dense-only collection must not look like
        # a passage index to callers that only inspect tensorFields.
        if not self._passage_write_errors(settings):
            settings["tensorFields"] = ["text_for_embedding"]
        return settings

    def get_stats(self, index: str) -> dict:
        try:
            info = self._live_collection_info(index)
            count = getattr(info, "points_count", None)
            if count is None and getattr(info, "result", None) is not None:
                count = getattr(info.result, "points_count", 0)
            return {
                "numberOfDocuments": int(count or 0),
                "numberOfVectors": int(count or 0),
                "backend": "qdrant",
            }
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def field_names(self, index: str) -> set[str]:
        try:
            settings = self.get_settings(index)
            names = {
                entry.get("name")
                for entry in (settings.get("allFields") or [])
                if entry.get("name")
            }
            return {name for name in names if name}
        except VectorStoreError:
            raise
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def index_exists(self, index: str) -> bool:
        try:
            names = {collection.name for collection in self.client().get_collections().collections}
            return index in names
        except Exception:
            return False

    def describe_index(self, index: str) -> IndexSchemaReport:
        if not self.index_exists(index):
            return IndexSchemaReport(exists=False)
        settings = self.get_settings(index)
        names = {
            entry.get("name")
            for entry in (settings.get("allFields") or [])
            if entry.get("name")
        }
        names = {name for name in names if name}

        # Every upsert writes both named vectors, so anything less than the exact
        # dense layout plus an IDF-modified bm25 vector cannot accept passage
        # writes. Reporting it as usable is what let the replacement path purge a
        # document and only then fail on the first upsert.
        errors = self._passage_write_errors(settings)
        tensor_fields: set[str] = set()
        if not errors:
            tensor_fields.update({"text_for_embedding", _DENSE_VECTOR, _SPARSE_VECTOR})

        live = {name for name in (settings.get("payload_schema_fields") or []) if name}
        expected_payload = {
            name for name, _ in _PAYLOAD_INDEXES
        } & core_passage_schema_field_names()
        missing_core = sorted(expected_payload - live)

        return IndexSchemaReport(
            exists=True,
            field_names=names,
            tensor_fields=tensor_fields,
            missing_core=missing_core,
            schema_errors=errors,
        )

    def _passage_write_errors(self, settings: dict) -> tuple[str, ...]:
        """Reasons this live collection cannot accept passage upserts."""
        dense = settings.get("dense") or {}
        sparse = settings.get("sparse") or {}
        errors: list[str] = []

        expected_size = dense.get("expected_size")
        if dense.get("size") is None:
            errors.append(
                f"missing dense vector named {_DENSE_VECTOR!r} "
                f"(found: {sorted((settings.get('vectors') or {}).keys())})"
            )
        elif dense.get("size") != expected_size:
            errors.append(
                f"dense vector {_DENSE_VECTOR!r} has size {dense.get('size')}, "
                f"embedder produces {expected_size}"
            )
        distance = str(dense.get("distance") or "").lower()
        if dense.get("size") is not None and distance not in {"dot", "dotproduct"}:
            errors.append(
                f"dense vector {_DENSE_VECTOR!r} uses distance {dense.get('distance')!r}, expected Dot"
            )
        comparator = str(dense.get("multivector_comparator") or "").lower()
        if comparator not in {"max_sim", "maxsim"}:
            errors.append(
                f"dense vector {_DENSE_VECTOR!r} has multivector comparator "
                f"{dense.get('multivector_comparator')!r}, expected MaxSim"
            )

        if not sparse.get("present"):
            errors.append(
                f"missing sparse vector named {_SPARSE_VECTOR!r} "
                f"(found: {sorted((settings.get('sparse_vectors') or {}).keys())})"
            )
        elif not sparse.get("idf"):
            errors.append(
                f"sparse vector {_SPARSE_VECTOR!r} has modifier {sparse.get('modifier')!r}, "
                "expected 'idf' — collection predates the BM25 IDF fix and needs recreate + reingest"
            )
        return tuple(errors)

    # -- writes --------------------------------------------------------------

    def create_index(self, index: str, settings: dict) -> None:
        from qdrant_client.http import models as rest

        try:
            dim = int(self.embedder.dense_dim)
            self.client().create_collection(
                collection_name=index,
                vectors_config={
                    _DENSE_VECTOR: rest.VectorParams(
                        size=dim,
                        distance=rest.Distance.DOT,
                        multivector_config=rest.MultiVectorConfig(
                            comparator=rest.MultiVectorComparator.MAX_SIM
                        ),
                        hnsw_config=rest.HnswConfigDiff(m=0),
                    ),
                },
                sparse_vectors_config={
                    _SPARSE_VECTOR: rest.SparseVectorParams(
                        modifier=rest.Modifier.IDF,
                        index=rest.SparseIndexParams(on_disk=False),
                    ),
                },
            )
            for field_name, schema in _PAYLOAD_INDEXES:
                payload_schema = {
                    "keyword": rest.PayloadSchemaType.KEYWORD,
                    "integer": rest.PayloadSchemaType.INTEGER,
                    "bool": rest.PayloadSchemaType.BOOL,
                    "text": rest.PayloadSchemaType.TEXT,
                    "float": rest.PayloadSchemaType.FLOAT,
                }[schema]
                try:
                    self.client().create_payload_index(
                        collection_name=index,
                        field_name=field_name,
                        field_schema=payload_schema,
                    )
                except Exception:
                    # Index may already exist on recreate races.
                    pass
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def add_documents(
        self,
        index: str,
        records: Sequence[dict],
        batch_size: int = 10,
        on_batch: Optional[Callable[[list[dict], dict], None]] = None,
    ) -> AddResult:
        from qdrant_client.http import models as rest

        if not records:
            return AddResult(batches=0, errors=[])
        if not self.index_exists(index):
            self.create_index(index, passage_index_settings())

        batches = 0
        all_errors: list[dict] = []
        size = max(1, int(batch_size))
        try:
            for start in range(0, len(records), size):
                batch = list(records[start : start + size])
                texts = []
                for record in batch:
                    text = record.get("text_for_embedding") or record.get("text") or ""
                    texts.append(str(text))
                # text_for_embedding is already "passage: …"; strip before
                # embed_passages so we do not double-prefix.
                bare = []
                for text in texts:
                    body = text
                    if body.lower().startswith("passage:"):
                        body = body.split(":", 1)[1].lstrip()
                    bare.append(body)
                windows_by_record = [sentence_windows(text) for text in bare]
                flat_windows = [window for windows in windows_by_record for window in windows]
                dense_encoder = getattr(self.embedder, "embed_passage_dense", None)
                dense_vectors = (
                    dense_encoder(flat_windows)
                    if callable(dense_encoder)
                    else self.embedder.embed_passages(flat_windows).dense
                )
                # Marqo's lexical arm searches both ``text`` and ``description``.
                # Keep both in the sparse representation while dense E5 remains
                # based only on the passage tensor field.
                lexical_texts = [
                    " ".join(
                        part.strip()
                        for part in (str(record.get("text") or body), str(record.get("description") or ""))
                        if part and part.strip()
                    )
                    for record, body in zip(batch, bare)
                ]
                sparse_list = self.embedder.embed_sparse_plain(lexical_texts)
                points = []
                vector_offset = 0
                for record, windows, sparse in zip(batch, windows_by_record, sparse_list):
                    dense_windows = dense_vectors[
                        vector_offset : vector_offset + len(windows)
                    ]
                    vector_offset += len(windows)
                    payload = _payload_from_record(record)
                    points.append(
                        rest.PointStruct(
                            id=point_id_for_record(record),
                            vector={
                                _DENSE_VECTOR: dense_windows,
                                _SPARSE_VECTOR: _sparse_to_qdrant(sparse),
                            },
                            payload=payload,
                        )
                    )
                self.client().upsert(collection_name=index, points=points, wait=True)
                batches += 1
                if on_batch is not None:
                    on_batch([], {"errors": False, "items": [{"status": 200} for _ in batch]})
        except Exception as error:
            raise VectorStoreError(str(error)) from error
        return AddResult(batches=batches, errors=all_errors)

    def delete_index(self, index: str) -> None:
        try:
            self.client().delete_collection(collection_name=index)
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def list_indexes(self) -> list[Any]:
        try:
            collections = self.client().get_collections().collections
            return [{"indexName": collection.name} for collection in collections]
        except Exception as error:
            raise VectorStoreError(str(error)) from error

    def update_documents(self, index: str, records: Sequence[dict]) -> Any:
        """Partial update compatible with Marqo ``update_documents``.

        Metadata-only patches (e.g. instance backfill) use ``set_payload`` so
        existing vectors/payload are preserved. Records that include text are
        merged with the live point before re-embedding, never replaced blank.
        """
        if not records:
            return {"updated": 0}
        if not self.index_exists(index):
            raise VectorStoreError(f"Index '{index}' not found")

        text_keys = {"text", "text_for_embedding"}
        payload_only: list[dict] = []
        with_text: list[dict] = []
        for record in records:
            if any(key in record and record.get(key) is not None for key in text_keys):
                with_text.append(record)
            else:
                payload_only.append(record)

        updated = 0
        if payload_only:
            for record in payload_only:
                point_id = point_id_for_record(record)
                payload = {
                    key: value
                    for key, value in record.items()
                    if key not in {"_id"}
                }
                if "query_enabled" in payload and payload.get("query_enabled") is not None:
                    payload["query_enabled"] = _as_bool(payload["query_enabled"])
                if "is_reference" in payload and payload.get("is_reference") is not None:
                    payload["is_reference"] = _as_bool(payload["is_reference"])
                record_id = str(record.get("_id") or record.get("record_id") or "").strip()
                if record_id:
                    payload["record_id"] = record_id
                _with_domain_tag_keys(payload)
                self.client().set_payload(
                    collection_name=index,
                    payload=payload,
                    points=[point_id],
                    wait=True,
                )
                updated += 1

        if with_text:
            merged_records: list[dict] = []
            for record in with_text:
                point_id = point_id_for_record(record)
                existing = self.client().retrieve(
                    collection_name=index,
                    ids=[point_id],
                    with_payload=True,
                    with_vectors=False,
                )
                if not existing:
                    raise VectorStoreError(f"Document '{record.get('_id')}' not found in '{index}'")
                merged: dict[str, Any] = {}
                if existing and existing[0].payload:
                    merged.update(dict(existing[0].payload))
                merged.update(record)
                if record.get("text") is not None and "text_for_embedding" not in record:
                    merged["text_for_embedding"] = ensure_passage_prefix(str(record["text"]))
                if "_id" not in merged and merged.get("record_id"):
                    merged["_id"] = merged["record_id"]
                merged_records.append(merged)
            result = self.add_documents(index, merged_records)
            updated += len(merged_records)
            return {"updated": updated, "batches": result.batches, "errors": result.errors}

        return {"updated": updated}

    def set_query_enabled(self, index: str, record_ids: Sequence[str], enabled: bool) -> dict:
        """Partial-update ``query_enabled`` on existing records. No re-embed.

        Same contract as Marqo ``set_query_enabled`` (PR #145). Metadata-only so
        :meth:`update_documents` uses ``set_payload`` rather than replacing points.
        """
        ids = list(dict.fromkeys(rid for rid in record_ids if rid))
        succeeded = []
        failed = []
        for rid in ids:
            try:
                point_id = record_id_to_point_id(rid)
                points = self.client().retrieve(
                    collection_name=index, ids=[point_id],
                    with_payload=["query_enabled"], with_vectors=False,
                )
                if not points:
                    raise VectorStoreError("record not found")
                self.update_documents(index, [{"_id": rid, "query_enabled": bool(enabled)}])
                points = self.client().retrieve(
                    collection_name=index, ids=[point_id],
                    with_payload=["query_enabled"], with_vectors=False,
                )
                if not points or (points[0].payload or {}).get("query_enabled") is not bool(enabled):
                    raise VectorStoreError("visibility update not confirmed")
                succeeded.append(rid)
            except Exception as error:
                failed.append({"_id": rid, "error": str(error)})
        return {"updated": len(succeeded), "failed": failed, "succeeded_ids": succeeded}

    def delete_chunk(
        self,
        document_id: str,
        chunk_num: int,
        index: str,
        workflow_id: Optional[str] = None,
    ) -> dict:
        try:
            if not self.index_exists(index):
                return {"deleted": False, "reason": "index_missing"}
            point_ids = self._purge_point_ids(
                index,
                document_id,
                workflow_id,
                extra_chunk_num=chunk_num,
                check_ambiguity=True,
                limit=10,
            )
            if not point_ids:
                return {"deleted": False, "reason": "not_found"}
            from qdrant_client.http import models as rest

            point_uuid = point_ids[0]
            self.client().delete(
                collection_name=index,
                points_selector=rest.PointIdsList(points=[point_uuid]),
                wait=True,
            )
            return {"deleted": True, "chunk_id": point_uuid}
        except Exception as error:
            return {"deleted": False, "error": str(error)}

    def delete_document(
        self, document_id: str, index: str, workflow_id: Optional[str] = None
    ) -> dict:
        from qdrant_client.http import models as rest

        try:
            if not self.index_exists(index):
                return {"deleted": 0, "doc_id": document_id, "reason": "index_missing"}
            deleted: list[str] = []
            seen: set[str] = set()
            first = True
            while True:
                ids = self._purge_point_ids(
                    index,
                    document_id,
                    workflow_id,
                    check_ambiguity=first,
                    limit=1000,
                )
                first = False
                fresh = [i for i in ids if i not in seen]
                if not fresh:
                    if ids:
                        return {
                            "deleted": len(deleted),
                            "doc_id": get_marqo_doc_id(document_id),
                            "error": "purge unconfirmed; points still match filter",
                        }
                    break
                self.client().delete(
                    collection_name=index,
                    points_selector=rest.PointIdsList(points=fresh),
                    wait=True,
                )
                deleted.extend(fresh)
                seen.update(fresh)
            return {"deleted": len(deleted), "doc_id": get_marqo_doc_id(document_id)}
        except Exception as error:
            return {"deleted": 0, "doc_id": document_id, "error": str(error)}

    def _purge_point_ids(
        self,
        index: str,
        document_id: str,
        workflow_id: Optional[str],
        *,
        extra_chunk_num: int | None = None,
        check_ambiguity: bool = True,
        limit: int = 1000,
    ) -> list[str]:
        from qdrant_client.http import models as rest

        doc_id = get_marqo_doc_id(document_id)

        def _scroll(scope_workflow: str | None) -> list[Any]:
            must = [
                rest.FieldCondition(key="doc_id", match=rest.MatchValue(value=doc_id)),
            ]
            if scope_workflow:
                must.append(
                    rest.FieldCondition(
                        key="workflow_id", match=rest.MatchValue(value=scope_workflow)
                    )
                )
            if extra_chunk_num is not None:
                must.append(
                    rest.FieldCondition(
                        key="chunk_num", match=rest.MatchValue(value=int(extra_chunk_num))
                    )
                )
            points, _ = self.client().scroll(
                collection_name=index,
                scroll_filter=rest.Filter(must=must),
                limit=limit,
                with_payload=["record_id", "doc_id", "workflow_id"],
                with_vectors=False,
            )
            return list(points or [])

        # Prefer scoped purge when workflow_id is known (same #73 rules as Marqo).
        if workflow_id:
            scoped = _scroll(workflow_id)
            if scoped or not check_ambiguity:
                return [_point_record_or_id(point) for point in scoped]
            strays = _scroll(None)
            if not strays:
                return []
            from .vector_store import MarqoPurgeScopeError

            raise MarqoPurgeScopeError(
                f"{len(strays)} record(s) share doc_id {doc_id} but none belong to "
                f"workflow {workflow_id}; refusing to purge."
            )
        return [_point_record_or_id(point) for point in _scroll(None)]


def _point_record_or_id(point) -> str:
    payload = point.payload or {}
    record_id = payload.get("record_id")
    if record_id:
        return record_id_to_point_id(str(record_id))
    return str(point.id)


def _looks_like_md5(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", value or ""))


def field_names_from_settings_safe(settings: dict) -> set[str]:
    from .vector_store import field_names_from_settings

    return field_names_from_settings(settings)
