"""Pure query preparation and result reranking helpers."""

import math
import re
from collections import Counter


_TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)


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
