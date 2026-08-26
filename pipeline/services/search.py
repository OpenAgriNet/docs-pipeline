"""Search ranking orchestration (query expand, E5 prefix, Marqo call, rerank).

Router owns auth + index access; this module owns ranking via ``vector_store``.
Algorithms are moved as-is — no ranking behavior change.

HTTP translation stays in the router: this module raises ``SearchServiceError``
only (framework-free service boundary).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Optional

from .. import db
from ..vector_store import (
    VectorStore,
    VectorStoreError,
    build_domain_tags_filter,
    get_vector_store,
    merge_filter_strings,
)

_TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)


class SearchServiceError(RuntimeError):
    """Domain failure from search orchestration (mapped to HTTP 400 by the router)."""

    pass


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def tokenize(value: str) -> list[str]:
    return _TOKEN_RE.findall(normalize_text(value))


def prepare_query_for_e5(query: str) -> str:
    cleaned = query.strip()
    if cleaned.lower().startswith("query:"):
        return cleaned
    return f"query: {cleaned}"


def token_overlap_score(query: str, text: str) -> float:
    query_tokens = set(tokenize(query))
    text_tokens = set(tokenize(text))
    if not query_tokens or not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def metadata_blob(hit: dict) -> str:
    return " ".join(
        str(hit.get(key) or "")
        for key in (
            "name",
            "name_en",
            "name_gu",
            "filename",
            "title_en",
            "title_gu",
            "category_tags",
            "description",
            "doc_short_description",
            "doc_llm_description",
        )
    )


def rank_desc(values: list[float]) -> list[int]:
    order = sorted(range(len(values)), key=lambda index: values[index], reverse=True)
    ranks = [0] * len(values)
    for position, index in enumerate(order, start=1):
        ranks[index] = position
    return ranks


def expand_query(query: str, profile: str) -> str:
    value = (query or "").strip()
    mode = (profile or "none").strip().lower()
    if not value or mode in {"none", ""}:
        return value
    if mode not in {"gu-v1", "gu_v1"}:
        return value

    rules = [
        (r"ખરવા|મોવાસા|fmd", "foot and mouth disease FMD blisters lesions mouth ulcer"),
        (r"આફરો|bloat", "ruminal bloat tympany frothy bloat"),
        (r"તાવ|fever", "pyrexia febrile infection"),
        (r"કબજ|constipation", "constipation bowel obstruction laxative"),
        (r"ગળિયો|ગળાની", "throat infection pharyngitis upper respiratory"),
        (r"કૃમિ|કરમિયા|deworm", "deworming helminth anthelmintic dose"),
        (r"ગર્ભપાત|ગાભણ", "abortion pregnancy gestation prenatal feeding"),
        (r"ચરમિયા|ચામડી|ખંજવાળ|hair fall", "dermatitis skin disease mange ectoparasite tick"),
    ]
    additions = [
        terms
        for pattern, terms in rules
        if re.search(pattern, value.lower(), flags=re.IGNORECASE)
    ]
    if not additions:
        return value
    return f"{value} {' '.join(additions)}".strip()


def bm25lite_scores(query: str, docs: list[str]) -> list[float]:
    query_tokens = tokenize(query)
    if not query_tokens or not docs:
        return [0.0] * len(docs)

    doc_tokens = [tokenize(document) for document in docs]
    average_length = max(
        1.0,
        sum(len(tokens) for tokens in doc_tokens) / max(1, len(doc_tokens)),
    )
    document_frequency: Counter[str] = Counter()
    for tokens in doc_tokens:
        for token in set(tokens):
            document_frequency[token] += 1

    k1 = 1.2
    b = 0.75
    scores: list[float] = []
    for tokens in doc_tokens:
        term_frequency = Counter(tokens)
        document_length = len(tokens)
        norm = k1 * (1 - b + b * document_length / average_length)
        score = 0.0
        for term in query_tokens:
            if term not in term_frequency:
                continue
            inverse_frequency = math.log(
                1.0
                + (len(doc_tokens) - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            )
            score += (
                inverse_frequency
                * (term_frequency[term] * (k1 + 1.0))
                / (term_frequency[term] + norm)
            )
        scores.append(score)
    return scores


def rerank_hits(query: str, hits: list[dict], rerank_mode: str) -> list[dict]:
    mode = (rerank_mode or "none").strip().lower()
    if mode in {"", "none"} or not hits:
        return hits

    raw_scores = [
        float(hit.get("_score", hit.get("score", 0.0)) or 0.0) for hit in hits
    ]
    minimum = min(raw_scores)
    maximum = max(raw_scores)
    denominator = (maximum - minimum) if maximum > minimum else 1.0
    semantic_scores = [(score - minimum) / denominator for score in raw_scores]
    text_scores = [
        token_overlap_score(query, str(hit.get("text") or "")) for hit in hits
    ]
    metadata_scores = [token_overlap_score(query, metadata_blob(hit)) for hit in hits]

    rescored: list[dict] = []
    if mode == "bm25lite":
        documents = [
            f"{str(hit.get('text') or '')} {metadata_blob(hit)}".strip()
            for hit in hits
        ]
        bm25_scores = bm25lite_scores(query, documents)
        bm25_minimum = min(bm25_scores) if bm25_scores else 0.0
        bm25_maximum = max(bm25_scores) if bm25_scores else 1.0
        bm25_denominator = (
            (bm25_maximum - bm25_minimum)
            if bm25_maximum > bm25_minimum
            else 1.0
        )
        normalized_bm25 = [
            (score - bm25_minimum) / bm25_denominator for score in bm25_scores
        ]
        for hit, semantic, bm25, metadata in zip(
            hits,
            semantic_scores,
            normalized_bm25,
            metadata_scores,
        ):
            enriched = dict(hit)
            enriched["_rerank_score"] = (
                (0.50 * semantic)
                + (0.40 * bm25)
                + (0.10 * metadata)
                + (-0.10 if bool(hit.get("is_reference", False)) else 0.0)
            )
            rescored.append(enriched)
    elif mode in {"rrf-lite", "rrf_lite", "rrf"}:
        semantic_rank = rank_desc(semantic_scores)
        text_rank = rank_desc(text_scores)
        metadata_rank = rank_desc(metadata_scores)
        k = 30
        for index, hit in enumerate(hits):
            enriched = dict(hit)
            enriched["_rerank_score"] = (
                (1.0 / (k + semantic_rank[index]))
                + (1.0 / (k + text_rank[index]))
                + (1.0 / (k + metadata_rank[index]))
            )
            rescored.append(enriched)
    else:
        for hit, semantic, text_score, metadata in zip(
            hits,
            semantic_scores,
            text_scores,
            metadata_scores,
        ):
            enriched = dict(hit)
            enriched["_rerank_score"] = (
                (0.60 * semantic)
                + (0.30 * max(text_score, metadata))
                + (0.10 * metadata)
            )
            rescored.append(enriched)

    rescored.sort(
        key=lambda hit: float(hit.get("_rerank_score", 0.0)),
        reverse=True,
    )
    return rescored


def empty_search_result(query: str, *, include_raw_hits: bool = False) -> dict[str, Any]:
    """Empty response when the caller has no searchable index of their own."""
    return {
        "effective_config": {
            "index_name": None,
            "query": query,
            "top_k": 0,
            "candidate_cap": 0,
            "filter_string": None,
        },
        "candidate_count": 0,
        "final_count": 0,
        "hits": [],
        "raw_hits": [] if include_raw_hits else None,
    }


def run_search(
    *,
    index_name: str,
    query: str,
    settings: dict,
    payload: dict,
    instance_filter: Optional[str] = None,
    store: Optional[VectorStore] = None,
) -> dict[str, Any]:
    """Execute Marqo search + rerank for an already-authorized physical index."""
    search_mode = (payload.get("search_mode") or settings.get("searchMethod") or "HYBRID").upper()
    top_k = max(1, min(int(payload.get("top_k") or settings.get("limit") or 12), 50))
    candidate_multiplier = max(
        1, int(payload.get("candidate_multiplier") or settings.get("candidateMultiplier") or 10)
    )
    requested_candidate_cap = payload.get("candidate_cap")
    if requested_candidate_cap is None:
        candidate_cap = min(
            max(top_k * candidate_multiplier, top_k),
            int(settings.get("candidateCap") or 120),
        )
    else:
        candidate_cap = int(requested_candidate_cap)
    candidate_cap = max(top_k, min(candidate_cap, 200))
    max_chunks_per_doc = max(
        1, int(payload.get("max_chunks_per_doc") or settings.get("maxChunksPerDoc") or 2)
    )
    use_e5_prefix = bool(payload.get("use_e5_prefix", settings.get("useE5Prefix", True)))
    exclude_reference = bool(
        payload.get("exclude_reference", settings.get("excludeReference", True))
    )
    alpha = float(payload.get("hybrid_alpha") or settings.get("alpha") or 0.6)
    ranking_method = payload.get("ranking_method") or settings.get("rankingMethod") or "rrf"
    ef_search = int(payload.get("ef_search") or settings.get("efSearch") or 256)
    query_expansion_profile = (
        payload.get("query_expansion_profile") or settings.get("queryExpansionProfile") or "gu-v1"
    )
    rerank_mode = payload.get("rerank_mode") or settings.get("rerankMode") or "none"
    hybrid_rrf_k = int(payload.get("hybrid_rrf_k") or settings.get("hybridRrfK") or 60)
    domain_tag_filters = payload.get("domain_tags") or payload.get("domain_tag_filters") or []
    if isinstance(domain_tag_filters, str):
        domain_tag_filters = [domain_tag_filters]
    expanded_query = expand_query(query, query_expansion_profile)
    effective_query = (
        prepare_query_for_e5(expanded_query) if use_e5_prefix else expanded_query
    )

    store = store or get_vector_store()

    request: dict[str, Any] = {
        "q": effective_query,
        "limit": candidate_cap,
        "search_method": search_mode.lower(),
        "ef_search": ef_search,
    }
    if exclude_reference:
        request["filter_string"] = "is_reference:false"
    if search_mode == "HYBRID":
        request["hybrid_parameters"] = {
            "alpha": alpha,
            "rankingMethod": ranking_method,
            "rrfK": hybrid_rrf_k,
            "searchableAttributesLexical": ["text", "description"],
            "searchableAttributesTensor": ["text_for_embedding"],
        }
    elif search_mode == "TENSOR":
        request["searchable_attributes"] = ["text_for_embedding"]
    else:
        request["searchable_attributes"] = ["text", "description"]

    reference_filter = "is_reference:false" if exclude_reference else None
    tag_filter = build_domain_tags_filter(domain_tag_filters)
    if tag_filter:
        try:
            field_names = store.field_names(index_name)
        except VectorStoreError as error:
            raise SearchServiceError(
                f"Unable to inspect index schema for '{index_name}': {error}"
            ) from error
        if "domain_tags" not in field_names:
            raise SearchServiceError(
                f"Index '{index_name}' does not support domain tag filters yet. "
                "Use an index created with the passage schema that includes 'domain_tags' "
                "(for example: documents-index-tags)."
            )
    filter_string = merge_filter_strings(reference_filter, tag_filter, instance_filter)
    if filter_string:
        request["filter_string"] = filter_string

    try:
        result = store.search(index_name, **request)
    except VectorStoreError as error:
        raise SearchServiceError(f"Marqo search failed: {error}") from error
    hits = result.get("hits", [])
    hits = rerank_hits(query, hits, rerank_mode)
    final_hits = []
    per_doc_counts: dict[str, int] = {}
    for hit in hits:
        doc_key = hit.get("doc_id") or hit.get("filename") or "__unknown__"
        if per_doc_counts.get(doc_key, 0) >= max_chunks_per_doc:
            continue
        per_doc_counts[doc_key] = per_doc_counts.get(doc_key, 0) + 1
        final_hits.append(hit)
        if len(final_hits) >= top_k:
            break

    for hit in final_hits:
        if hit.get("domain_tags"):
            continue
        doc_id = hit.get("doc_id")
        chunk_num = (
            hit.get("chunk_num") if hit.get("chunk_num") is not None else hit.get("chunk_number")
        )
        if not doc_id or chunk_num is None:
            continue
        flat_tags = db.get_domain_tags_flat_for_document_chunk(str(doc_id), int(chunk_num))
        if flat_tags:
            hit["domain_tags"] = flat_tags
            hit["domain_tags_source"] = "sqlite"

    return {
        "effective_config": {
            "index_name": index_name,
            "query": query,
            "search_mode": search_mode,
            "top_k": top_k,
            "candidate_cap": candidate_cap,
            "candidate_multiplier": candidate_multiplier,
            "max_chunks_per_doc": max_chunks_per_doc,
            "use_e5_prefix": use_e5_prefix,
            "exclude_reference": exclude_reference,
            "hybrid_alpha": alpha,
            "ranking_method": ranking_method,
            "hybrid_rrf_k": hybrid_rrf_k,
            "ef_search": ef_search,
            "query_expansion_profile": query_expansion_profile,
            "query_expansion_applied": expanded_query != query,
            "rerank_mode": rerank_mode,
            "domain_tags": list(domain_tag_filters) if domain_tag_filters else [],
            "filter_string": filter_string,
        },
        "candidate_count": len(hits),
        "final_count": len(final_hits),
        "hits": final_hits,
        "raw_hits": hits if payload.get("include_raw_hits") else None,
    }
