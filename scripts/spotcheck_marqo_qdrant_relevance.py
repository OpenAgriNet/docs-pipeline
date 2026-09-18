#!/usr/bin/env python3
"""Side-by-side relevance spot-check (read-only)."""
from __future__ import annotations

import json
import os
import re
import urllib.request

MARQO = os.environ.get("MARQO_URL", "http://127.0.0.1:8882").rstrip("/")
QDRANT = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
MINDEX = "amul-veterinary-rebuild-20260821"
QINDEX = "amul-veterinary-rebuild-20260821-qdrant"

QUERIES = [
    "SPNF Palekar Devvrat natural farming methods",
    "jeevamrut preparation natural farming",
    "foot and mouth disease blisters mouth cattle",
    "subscriber notice printer magazine committee",
]


def snip(text: str, n: int = 180) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:n]


def marqo_search(q: str, limit: int = 5) -> list[dict]:
    body = {
        "q": f"query: {q}",
        "limit": limit,
        "searchMethod": "HYBRID",
        "filter": "is_reference:false",
        "attributesToRetrieve": ["filename", "workflow_id", "chunk_num", "text", "is_reference"],
        "efSearch": 256,
        "hybridParameters": {
            "alpha": 0.6,
            "rankingMethod": "rrf",
            "rrfK": 60,
            "searchableAttributesLexical": ["text", "description"],
            "searchableAttributesTensor": ["text_for_embedding"],
        },
    }
    req = urllib.request.Request(
        f"{MARQO}/indexes/{MINDEX}/search",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return list(json.loads(resp.read().decode()).get("hits") or [])


def qdrant_search(store, q: str, limit: int = 5) -> list[dict]:
    return list(
        store.search(
            QINDEX,
            q=f"query: {q}",
            limit=limit,
            search_method="hybrid",
            filter_string="is_reference:false",
            hybrid_parameters={"rrfK": 60, "alpha": 0.6},
        ).get("hits")
        or []
    )


def relevantish(query: str, hit: dict) -> str:
    blob = f"{hit.get('filename') or ''} {hit.get('text') or ''}".lower()
    cues = {
        "palekar": ("palekar", "spnf", "jeevamrut", "natural farming", "devvrat", "beejamrut"),
        "jeevamrut": ("jeevamrut", "beejamrut", "ghanjeevamrut", "palekar", "natural farming"),
        "foot": ("foot and mouth", "fmd", "blister", "mouth", "ulcer"),
        "subscriber": ("subscriber", "printer", "magazine", "committee", "advertisement"),
    }
    key = "palekar"
    if "jeevamrut" in query.lower():
        key = "jeevamrut"
    elif "foot" in query.lower():
        key = "foot"
    elif "subscriber" in query.lower():
        key = "subscriber"
    hits = sum(1 for c in cues[key] if c in blob)
    if key == "subscriber":
        return "JUNKISH" if hits else "OTHER"
    return "RELEVANT" if hits else "WEAK/OFF"


def main() -> None:
    os.environ.setdefault("EMBEDDING_BACKEND", "fastembed")
    os.environ.setdefault("EMBEDDING_NORMALIZE", "true")
    os.environ.setdefault("VECTOR_STORE_BACKEND", "qdrant")
    from pipeline.vector_store_qdrant import QdrantStore

    store = QdrantStore(url=QDRANT)
    for q in QUERIES:
        print("=" * 72)
        print("QUERY:", q)
        mh = marqo_search(q)
        qh = qdrant_search(store, q)
        print("--- Marqo top ---")
        for i, h in enumerate(mh, 1):
            tag = relevantish(q, h)
            print(f"  M{i} [{tag}] {h.get('filename')}#{h.get('chunk_num')} ref={h.get('is_reference')}")
            print(f"      {snip(h.get('text'))}")
        print("--- Qdrant top ---")
        for i, h in enumerate(qh, 1):
            tag = relevantish(q, h)
            print(f"  Q{i} [{tag}] {h.get('filename')}#{h.get('chunk_num')} ref={h.get('is_reference')}")
            print(f"      {snip(h.get('text'))}")
        m_rel = sum(1 for h in mh if relevantish(q, h) in {"RELEVANT", "JUNKISH"})
        q_rel = sum(1 for h in qh if relevantish(q, h) in {"RELEVANT", "JUNKISH"})
        print(f"cue_hits_in_top5 marqo={m_rel}/5 qdrant={q_rel}/5")


if __name__ == "__main__":
    main()
