#!/usr/bin/env python3
"""Seed labeled qrels for Marqo vs Qdrant eval (#4) from live Marqo LEXICAL hits.

Writes JSONL: query_id, query, relevant keys (workflow_id|chunk_num), grade.
Manual review can edit later for the full eval plan.
"""
from __future__ import annotations

import json
import re
import urllib.request

MARQO = "http://127.0.0.1:8882"
INDEX = "amul-veterinary-rebuild-20260821"
OUT = "/tmp/qrels_seed.jsonl"

SEEDS = [
    ("spnf_jeevamrut", "jeevamrut palekar natural farming preparation", ("jeevamrut", "palekar", "beejamrut", "ghanjeevamrut")),
    ("spnf_farmer", "SPNF Palekar Devvrat natural farming methods", ("palekar", "spnf", "devvrat", "natural farming", "jeevamrut")),
    ("spnf_beejamrut", "beejamrut ghanjeevamrut how to prepare", ("beejamrut", "ghanjeevamrut", "jeevamrut")),
    ("fmd_en", "foot and mouth disease blisters mouth cattle", ("foot and mouth", "fmd", "blister", "mouth")),
    ("fmd_gu", "ખરવા મોવાસા ગાય", ("ખરવા", "મોવાસા", "fmd", "blister")),
    ("bloat_en", "ruminal bloat tympany frothy bloat cattle", ("bloat", "tympany", "ruminal")),
    ("fever_en", "cattle fever pyrexia treatment", ("fever", "pyrexia", "febrile")),
    ("deworm_en", "deworming helminth anthelmintic dose cattle", ("deworm", "helminth", "anthelmintic", "worm")),
    ("mastitis_en", "mastitis milk udder infection treatment", ("mastitis", "udder", "milk")),
    ("calving_en", "calving dystocia difficult birth cattle", ("calving", "dystocia", "birth")),
    ("skin_en", "dermatitis mange tick skin disease cattle", ("dermatitis", "mange", "tick", "skin")),
    ("abortion_en", "abortion pregnancy cattle gestation", ("abortion", "pregnancy", "gestation")),
    ("nutrition_en", "cattle feed ration protein energy fodder", ("feed", "ration", "fodder", "protein")),
    ("vaccine_en", "cattle vaccination schedule FMD HS BQ", ("vaccin", "fmd", "hs", "bq")),
    ("gu_fever", "ગાયને તાવ આવે તો શું કરવું", ("તાવ", "fever", "ગાય")),
    ("gu_worm", "ગાયમાં કૃમિ કરમિયા દવા", ("કૃમિ", "કરમિયા", "worm", "deworm")),
    ("junk_subscriber", "subscriber notice printer magazine committee", ("subscriber", "printer", "committee", "magazine")),
    ("junk_toc", "table of contents editorial board index page", ("contents", "editorial", "index", "committee")),
    ("zero_budget", "zero budget natural farming Subhash Palekar", ("zero budget", "palekar", "natural farming")),
    ("krushi_govidya", "Krushigovidya jeevamrut article", ("krushigovidya", "jeevamrut", "palekar")),
]


def search(q: str, limit: int = 25) -> list[dict]:
    body = {
        "q": q,
        "limit": limit,
        "searchMethod": "LEXICAL",
        "filter": "is_reference:false",
        "attributesToRetrieve": [
            "workflow_id",
            "chunk_num",
            "filename",
            "text",
            "is_reference",
            "doc_id",
        ],
    }
    req = urllib.request.Request(
        f"{MARQO}/indexes/{INDEX}/search",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return list(json.loads(resp.read().decode()).get("hits") or [])


def key(hit: dict) -> str:
    return f"{hit.get('workflow_id') or hit.get('doc_id')}|{hit.get('chunk_num')}"


def blob(hit: dict) -> str:
    return f"{hit.get('filename') or ''} {hit.get('text') or ''}".lower()


def main() -> None:
    rows = []
    for qid, query, must_any in SEEDS:
        hits = search(query)
        relevant = []
        for hit in hits:
            b = blob(hit)
            if any(tok.lower() in b for tok in must_any):
                relevant.append(
                    {
                        "key": key(hit),
                        "grade": 2 if sum(1 for t in must_any if t.lower() in b) >= 2 else 1,
                        "filename": hit.get("filename"),
                        "snippet": re.sub(r"\s+", " ", str(hit.get("text") or ""))[:160],
                    }
                )
            if len(relevant) >= 3:
                break
        # For junk queries, prefer hits that LOOK like junk (grade still 1 for "expected junk surface")
        # Full eval will separate junk preference; here we still need at least one anchor if possible.
        if not relevant and hits:
            for hit in hits[:2]:
                relevant.append(
                    {
                        "key": key(hit),
                        "grade": 1,
                        "filename": hit.get("filename"),
                        "snippet": re.sub(r"\s+", " ", str(hit.get("text") or ""))[:160],
                        "note": "fallback_top_lexical",
                    }
                )
        row = {
            "query_id": qid,
            "query": query,
            "filter": "is_reference:false",
            "relevant": relevant,
            "junk_query": qid.startswith("junk_"),
        }
        rows.append(row)
        print(f"{qid}: {len(relevant)} relevant from {len(hits)} hits")

    with open(OUT, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} n={len(rows)}")


if __name__ == "__main__":
    main()
