#!/usr/bin/env python3
"""Reproduce issue #150 against live Marqo (pipeline-equivalent search path).

Does NOT go through amul-oan-api. It does use the same ranking knobs:
hybrid + gu-v1 expansion + E5 query prefix + is_reference filter +
candidate cap 120 + bm25lite rerank + max 2 chunks/doc.
"""
from __future__ import annotations

import json
import math
import re
import sys
import urllib.error
import urllib.request
from collections import Counter

MARQO_URL = "http://127.0.0.1:8882"
INDEX = "amul-veterinary-index-restored"
CANDIDATE_CAP = 120
TOP_K = 12
MAX_CHUNKS_PER_DOC = 2
ALPHA = 0.6
RRF_K = 60

_TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)
EXPAND_RULES = [
    (r"ખરવા|મોવાસા|fmd", "foot and mouth disease FMD blisters lesions mouth ulcer"),
    (r"આફરો|bloat", "ruminal bloat tympany frothy bloat"),
    (r"તાવ|fever", "pyrexia febrile infection"),
    (r"કબજ|constipation", "constipation bowel obstruction laxative"),
    (r"ગળિયો|ગળાની", "throat infection pharyngitis upper respiratory"),
    (r"કૃમિ|કરમિયા|deworm", "deworming helminth anthelmintic dose"),
    (r"ગર્ભપાત|ગાભણ", "abortion pregnancy gestation prenatal feeding"),
    (r"ચરમિયા|ચામડી|ખંજવાળ|hair fall", "dermatitis skin disease mange ectoparasite tick"),
]
INTERESTING = (
    "krushigovidya",
    "gaudhuli",
    "palekar",
    "devvrat",
    "spnf",
    "beejamrut",
    "jeevamrut",
    "ghanjeevamrut",
    "zero budget",
    "natural farming",
    "pasudhan",
)


def tokenize(value: str) -> list[str]:
    return _TOKEN_RE.findall(re.sub(r"\s+", " ", (value or "").strip().lower()))


def expand_query(query: str) -> str:
    additions = [
        terms
        for pattern, terms in EXPAND_RULES
        if re.search(pattern, query.lower(), flags=re.IGNORECASE)
    ]
    if not additions:
        return query
    return f"{query} {' '.join(additions)}".strip()


def bm25lite_scores(query: str, docs: list[str]) -> list[float]:
    query_tokens = tokenize(query)
    if not query_tokens or not docs:
        return [0.0] * len(docs)
    doc_tokens = [tokenize(document) for document in docs]
    average_length = max(1.0, sum(len(t) for t in doc_tokens) / max(1, len(doc_tokens)))
    document_frequency: Counter[str] = Counter()
    for tokens in doc_tokens:
        for token in set(tokens):
            document_frequency[token] += 1
    k1, b = 1.2, 0.75
    scores = []
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
            score += inverse_frequency * (term_frequency[term] * (k1 + 1.0)) / (
                term_frequency[term] + norm
            )
        scores.append(score)
    return scores


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


def rerank_bm25lite(query: str, hits: list[dict]) -> list[dict]:
    if not hits:
        return hits
    raw_scores = [float(hit.get("_score", hit.get("score", 0.0)) or 0.0) for hit in hits]
    minimum, maximum = min(raw_scores), max(raw_scores)
    denominator = (maximum - minimum) if maximum > minimum else 1.0
    semantic_scores = [(score - minimum) / denominator for score in raw_scores]
    documents = [f"{str(hit.get('text') or '')} {metadata_blob(hit)}".strip() for hit in hits]
    bm25_scores = bm25lite_scores(query, documents)
    bm25_minimum, bm25_maximum = min(bm25_scores), max(bm25_scores)
    bm25_denominator = (bm25_maximum - bm25_minimum) if bm25_maximum > bm25_minimum else 1.0
    normalized_bm25 = [(score - bm25_minimum) / bm25_denominator for score in bm25_scores]
    metadata_scores = [
        len(set(tokenize(query)) & set(tokenize(metadata_blob(hit)))) / max(1, len(set(tokenize(query))))
        for hit in hits
    ]
    rescored = []
    for hit, semantic, bm25, metadata in zip(hits, semantic_scores, normalized_bm25, metadata_scores):
        enriched = dict(hit)
        enriched["_rerank_score"] = (
            (0.50 * semantic)
            + (0.40 * bm25)
            + (0.10 * metadata)
            + (-0.10 if bool(hit.get("is_reference", False)) else 0.0)
        )
        rescored.append(enriched)
    rescored.sort(key=lambda hit: float(hit.get("_rerank_score", 0.0)), reverse=True)
    return rescored


def trim_per_doc(hits: list[dict], top_k: int = TOP_K) -> list[dict]:
    final = []
    per_doc: dict[str, int] = {}
    for hit in hits:
        doc_key = hit.get("doc_id") or hit.get("filename") or "__unknown__"
        if per_doc.get(doc_key, 0) >= MAX_CHUNKS_PER_DOC:
            continue
        per_doc[doc_key] = per_doc.get(doc_key, 0) + 1
        final.append(hit)
        if len(final) >= top_k:
            break
    return final


def marqo_search(q: str, method: str) -> dict:
    body: dict = {
        "q": q,
        "limit": CANDIDATE_CAP,
        "searchMethod": method.upper(),
        "filter": "is_reference:false",
        "attributesToRetrieve": [
            "filename",
            "name_en",
            "title_en",
            "doc_id",
            "workflow_id",
            "text",
            "description",
            "is_reference",
            "chunk_num",
        ],
    }
    if method.upper() in {"TENSOR", "HYBRID"}:
        body["efSearch"] = 256
    if method.upper() == "HYBRID":
        body["hybridParameters"] = {
            "alpha": ALPHA,
            "rankingMethod": "rrf",
            "rrfK": RRF_K,
            "searchableAttributesLexical": ["text", "description"],
            "searchableAttributesTensor": ["text_for_embedding"],
        }
    elif method.upper() == "TENSOR":
        body["searchableAttributes"] = ["text_for_embedding"]
    else:
        body["searchableAttributes"] = ["text", "description"]
    req = urllib.request.Request(
        f"{MARQO_URL}/indexes/{INDEX}/search",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"Marqo {exc.code} for {method}: {detail[:800]}") from exc


def hit_name(hit: dict) -> str:
    return str(hit.get("filename") or hit.get("name_en") or hit.get("title_en") or "?")


def snippet(hit: dict, n: int = 140) -> str:
    text = re.sub(r"\s+", " ", str(hit.get("text") or ""))
    return text[:n]


def is_interesting(hit: dict) -> bool:
    blob = f"{hit_name(hit)} {hit.get('text') or ''} {metadata_blob(hit)}".lower()
    return any(token in blob for token in INTERESTING)


def summarize(label: str, query: str, hits: list[dict], extra: str = "") -> None:
    interesting = [h for h in hits if is_interesting(h)]
    print(f"\n=== {label} ===")
    print(f"query: {query}")
    if extra:
        print(extra)
    print(f"hits={len(hits)} interesting={len(interesting)}")
    for i, hit in enumerate(hits[:8], 1):
        mark = " *" if is_interesting(hit) else ""
        score = hit.get("_rerank_score", hit.get("_score", hit.get("score")))
        print(f"  {i:2d}.{mark} score={score}  {hit_name(hit)}")
        print(f"      {snippet(hit)}")
    if interesting:
        print("  interesting filenames:")
        seen = []
        for hit in interesting:
            name = hit_name(hit)
            if name not in seen:
                seen.append(name)
        for name in seen[:12]:
            print(f"    - {name}")
    else:
        print("  NO Palekar/SPNF/jeevamrut-style hits in this list")


def run_case(label: str, farmer_query: str, method: str, expand: bool, rerank: bool) -> None:
    q = expand_query(farmer_query) if expand else farmer_query
    search_q = q
    if method.upper() in {"TENSOR", "HYBRID"}:
        if not search_q.lower().startswith("query:"):
            search_q = f"query: {search_q}"
    result = marqo_search(search_q, method)
    hits = result.get("hits") or []
    extra = f"expanded={expand!s} rerank={rerank!s} raw_hits={len(hits)}"
    if q != farmer_query:
        extra += f"\nexpanded_to: {q}"
    ranked = rerank_bm25lite(farmer_query, hits) if rerank else hits
    final = trim_per_doc(ranked)
    summarize(f"{label} CANDIDATES (cap {CANDIDATE_CAP})", farmer_query, ranked[:20], extra)
    summarize(f"{label} FINAL (top {TOP_K}, max {MAX_CHUNKS_PER_DOC}/doc)", farmer_query, final)


def main() -> None:
    farmer = "SPNF Palekar Devvrat natural farming methods"
    lexical_known = "jeevamrut beejamrut ghanjeevamrut natural farming preparation"
    print(f"Marqo {MARQO_URL} index={INDEX}")
    run_case("A lexical known-good", lexical_known, "LEXICAL", expand=False, rerank=False)
    run_case("B farmer LEXICAL", farmer, "LEXICAL", expand=False, rerank=False)
    run_case("C farmer TENSOR+e5", farmer, "TENSOR", expand=False, rerank=False)
    run_case("D farmer HYBRID no expand", farmer, "HYBRID", expand=False, rerank=False)
    run_case("E farmer HYBRID+gu-v1", farmer, "HYBRID", expand=True, rerank=False)
    run_case("F FULL prod-like hybrid+expand+bm25lite", farmer, "HYBRID", expand=True, rerank=True)


if __name__ == "__main__":
    sys.exit(main())
