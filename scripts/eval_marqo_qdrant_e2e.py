#!/usr/bin/env python3
"""Full end-to-end Marqo vs Qdrant eval via prod search path (#151).

Builds consensus qrels (not Marqo-lexical-only), then scores four arms through
``pipeline.services.search.run_search``:

  marqo_hybrid / marqo_hybrid_bm25lite / qdrant_hybrid / qdrant_hybrid_bm25lite

Does NOT change VECTOR_STORE_BACKEND or touch prod config.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
from typing import Any

QUERIES: list[tuple[str, str, tuple[str, ...], bool]] = [
    # query_id, query, must_any cues, is_junk
    ("spnf_jeevamrut", "jeevamrut palekar natural farming preparation", ("jeevamrut", "palekar", "beejamrut"), False),
    ("spnf_farmer", "SPNF Palekar Devvrat natural farming methods", ("palekar", "spnf", "devvrat", "jeevamrut", "natural farming"), False),
    ("spnf_beejamrut", "beejamrut ghanjeevamrut how to prepare", ("beejamrut", "ghanjeevamrut", "jeevamrut"), False),
    ("zero_budget", "zero budget natural farming Subhash Palekar", ("zero budget", "palekar", "natural farming"), False),
    ("krushi_govidya", "Krushigovidya jeevamrut article", ("krushigovidya", "jeevamrut", "palekar"), False),
    ("fmd_en", "foot and mouth disease blisters mouth cattle", ("foot and mouth", "fmd", "blister", "mouth"), False),
    ("fmd_gu", "ખરવા મોવાસા ગાય", ("ખરવા", "મોવાસા", "fmd", "blister"), False),
    ("bloat_en", "ruminal bloat tympany frothy bloat cattle", ("bloat", "tympany", "ruminal"), False),
    ("fever_en", "cattle fever pyrexia treatment", ("fever", "pyrexia", "febrile"), False),
    ("deworm_en", "deworming helminth anthelmintic dose cattle", ("deworm", "helminth", "anthelmintic", "worm"), False),
    ("mastitis_en", "mastitis milk udder infection treatment", ("mastitis", "udder"), False),
    ("calving_en", "calving dystocia difficult birth cattle", ("calving", "dystocia", "birth"), False),
    ("skin_en", "dermatitis mange tick skin disease cattle", ("dermatitis", "mange", "tick", "skin"), False),
    ("abortion_en", "abortion pregnancy cattle gestation", ("abortion", "pregnancy", "gestation"), False),
    ("nutrition_en", "cattle feed ration protein energy fodder", ("feed", "ration", "fodder", "protein"), False),
    ("vaccine_en", "cattle vaccination schedule FMD HS BQ", ("vaccin", "fmd", "hs", "bq"), False),
    ("gu_fever", "ગાયને તાવ આવે તો શું કરવું", ("તાવ", "fever", "ગાય"), False),
    ("gu_worm", "ગાયમાં કૃમિ કરમિયા દવા", ("કૃમિ", "કરમિયા", "worm", "deworm"), False),
    ("junk_subscriber", "subscriber notice printer magazine committee", ("subscriber", "printer", "committee", "magazine"), True),
    ("junk_toc", "table of contents editorial board index page", ("contents", "editorial", "index", "committee"), True),
]

SPNF_IDS = {"spnf_jeevamrut", "spnf_farmer", "spnf_beejamrut", "zero_budget", "krushi_govidya"}

SETTINGS = {
    "searchMethod": "HYBRID",
    "limit": 10,
    "candidateCap": 30,
    "candidateMultiplier": 3,
    "maxChunksPerDoc": 2,
    "useE5Prefix": True,
    "excludeReference": True,
    "alpha": 0.6,
    "rankingMethod": "rrf",
    "efSearch": 256,
    "queryExpansionProfile": "gu-v1",
    "rerankMode": "none",
    "hybridRrfK": 60,
}


def hit_key(hit: dict) -> str:
    wf = hit.get("workflow_id") or hit.get("doc_id") or "?"
    return f"{wf}|{hit.get('chunk_num')}"


def blob(hit: dict) -> str:
    return f"{hit.get('filename') or ''} {hit.get('text') or ''} {hit.get('description') or ''}".lower()


def recall_at(keys: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(keys[:k]) & relevant) / len(relevant)


def mrr(keys: list[str], relevant: set[str]) -> float:
    for i, key in enumerate(keys, start=1):
        if key in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at(keys: list[str], grades: dict[str, int], k: int) -> float:
    if not grades:
        return 0.0

    def dcg(vals: list[str]) -> float:
        total = 0.0
        for i, key in enumerate(vals[:k], start=1):
            rel = grades.get(key, 0)
            if rel:
                total += (2**rel - 1) / math.log2(i + 1)
        return total

    ideal = sorted(grades.values(), reverse=True)
    idcg = sum((2**rel - 1) / math.log2(i + 1) for i, rel in enumerate(ideal[:k], start=1))
    return 0.0 if idcg <= 0 else dcg(keys) / idcg


def mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def raw_search(store, index: str, query: str, method: str, limit: int = 25) -> list[dict]:
    from pipeline.services.search import prepare_query_for_e5

    q = prepare_query_for_e5(query) if method.upper() in {"TENSOR", "HYBRID"} else query
    request: dict[str, Any] = {
        "q": q,
        "limit": limit,
        "search_method": method.lower(),
        "filter_string": "is_reference:false",
    }
    if method.upper() in {"TENSOR", "HYBRID"}:
        request["ef_search"] = 256
    if method.upper() == "HYBRID":
        request["hybrid_parameters"] = {
            "alpha": 0.6,
            "rankingMethod": "rrf",
            "rrfK": 60,
            "searchableAttributesLexical": ["text", "description"],
            "searchableAttributesTensor": ["text_for_embedding"],
        }
    elif method.upper() == "TENSOR":
        request["searchable_attributes"] = ["text_for_embedding"]
    else:
        request["searchable_attributes"] = ["text", "description"]
    return list(store.search(index, **request).get("hits") or [])


def build_consensus_qrels(marqo_store, qdrant_store, marqo_index: str, qdrant_index: str) -> list[dict]:
    """Label = cue-matching hits that appear in Marqo LEXICAL or both HYBRID lists."""
    rows = []
    for qid, query, cues, is_junk in QUERIES:
        m_lex = raw_search(marqo_store, marqo_index, query, "LEXICAL", 25)
        m_hyb = raw_search(marqo_store, marqo_index, query, "HYBRID", 25)
        q_hyb = raw_search(qdrant_store, qdrant_index, query, "HYBRID", 25)
        m_hyb_keys = {hit_key(h) for h in m_hyb}
        q_hyb_keys = {hit_key(h) for h in q_hyb}
        consensus_pool = {hit_key(h) for h in m_lex} | (m_hyb_keys & q_hyb_keys)

        by_key: dict[str, dict] = {}
        for hit in m_lex + m_hyb + q_hyb:
            by_key.setdefault(hit_key(hit), hit)

        relevant = []
        for key in consensus_pool:
            hit = by_key.get(key)
            if not hit:
                continue
            b = blob(hit)
            cue_hits = sum(1 for c in cues if c.lower() in b)
            if cue_hits == 0 and not is_junk:
                continue
            if is_junk:
                # For junk queries, prefer cue-matching junkish docs as "expected surface"
                if cue_hits == 0:
                    continue
            grade = 2 if cue_hits >= 2 or key in (m_hyb_keys & q_hyb_keys) else 1
            relevant.append(
                {
                    "key": key,
                    "grade": grade,
                    "filename": hit.get("filename"),
                    "snippet": re.sub(r"\s+", " ", str(hit.get("text") or ""))[:140],
                    "in_both_hybrid": key in (m_hyb_keys & q_hyb_keys),
                }
            )
        relevant.sort(key=lambda r: (-r["grade"], -int(r["in_both_hybrid"]), r["key"]))
        relevant = relevant[:3]
        rows.append(
            {
                "query_id": qid,
                "query": query,
                "junk_query": is_junk,
                "spnf": qid in SPNF_IDS,
                "relevant": relevant,
            }
        )
        print(f"  qrel {qid}: {len(relevant)} labels")
    return rows


def run_arm(store, index: str, query: str, rerank_mode: str) -> tuple[list[str], float]:
    from pipeline.services.search import run_search

    payload = {
        "search_mode": "HYBRID",
        "top_k": 10,
        "candidate_cap": 30,
        "exclude_reference": True,
        "use_e5_prefix": True,
        "hybrid_alpha": 0.6,
        "hybrid_rrf_k": 60,
        "rerank_mode": rerank_mode,
        "query_expansion_profile": "gu-v1",
        "max_chunks_per_doc": 2,
    }
    t0 = time.perf_counter()
    result = run_search(
        index_name=index,
        query=query,
        settings=SETTINGS,
        payload=payload,
        store=store,
    )
    ms = (time.perf_counter() - t0) * 1000
    hits = result.get("hits") or result.get("results") or []
    # run_search returns final_hits under "hits"
    if isinstance(result, dict) and "hits" not in result:
        hits = result.get("documents") or []
    return [hit_key(h) for h in hits], ms


def score_rows(qrels: list[dict], arm_results: dict[str, dict[str, list[str]]]) -> dict[str, Any]:
    arms = list(arm_results.keys())
    metrics = {a: {"recall@5": [], "recall@10": [], "mrr": [], "ndcg@10": []} for a in arms}
    per_query = []
    for row in qrels:
        if row.get("junk_query"):
            continue
        grades = {r["key"]: int(r["grade"]) for r in row["relevant"]}
        relevant = set(grades)
        if not relevant:
            continue
        pq = {"query_id": row["query_id"], "spnf": row.get("spnf"), "n_relevant": len(relevant)}
        for arm in arms:
            keys = arm_results[arm].get(row["query_id"], [])
            metrics[arm]["recall@5"].append(recall_at(keys, relevant, 5))
            metrics[arm]["recall@10"].append(recall_at(keys, relevant, 10))
            metrics[arm]["mrr"].append(mrr(keys, relevant))
            metrics[arm]["ndcg@10"].append(ndcg_at(keys, grades, 10))
            pq[arm] = {
                "recall@10": recall_at(keys, relevant, 10),
                "mrr": mrr(keys, relevant),
                "ndcg@10": ndcg_at(keys, grades, 10),
            }
        per_query.append(pq)

    summary = {a: {k: round(mean(v), 4) for k, v in metrics[a].items()} for a in arms}
    # SPNF subset
    spnf_rows = [p for p in per_query if p.get("spnf")]
    spnf_summary = {}
    for arm in arms:
        spnf_summary[arm] = {
            "recall@10": round(mean([p[arm]["recall@10"] for p in spnf_rows]), 4) if spnf_rows else 0.0,
            "mrr": round(mean([p[arm]["mrr"] for p in spnf_rows]), 4) if spnf_rows else 0.0,
        }
    return {"summary": summary, "spnf_summary": spnf_summary, "per_query": per_query}


def decide(summary: dict, spnf: dict) -> dict[str, Any]:
    m = summary["marqo_hybrid"]["recall@10"]
    # best qdrant arm
    q_arms = ["qdrant_hybrid", "qdrant_hybrid_bm25lite"]
    best = max(q_arms, key=lambda a: (summary[a]["recall@10"], summary[a]["ndcg@10"], summary[a]["mrr"]))
    delta = summary[best]["recall@10"] - m
    spnf_ok = spnf[best]["recall@10"] + 1e-9 >= spnf["marqo_hybrid"]["recall@10"] - 0.05
    ok = delta >= -0.05 and spnf_ok
    return {
        "best_qdrant_arm": best,
        "delta_recall@10": round(delta, 4),
        "spnf_ok": spnf_ok,
        "pass_bar": ok,
        "bar": "Qdrant R@10 >= Marqo-0.05 AND SPNF/#150 R@10 does not regress >0.05",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marqo-url", default=os.environ.get("MARQO_URL", "http://marqo:8882"))
    parser.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL", "http://qdrant:6333"))
    parser.add_argument("--marqo-index", default=os.environ.get("MARQO_AB_INDEX", "amul-veterinary-rebuild-20260821"))
    parser.add_argument(
        "--qdrant-index",
        default=os.environ.get("QDRANT_AB_INDEX", "amul-veterinary-rebuild-20260821-qdrant"),
    )
    parser.add_argument("--json-out", default="/tmp/eval_marqo_qdrant_e2e.json")
    parser.add_argument("--qrels-out", default="/tmp/qrels_consensus.jsonl")
    args = parser.parse_args()

    os.environ.setdefault("EMBEDDING_BACKEND", "fastembed")
    os.environ.setdefault("EMBEDDING_NORMALIZE", "true")
    os.environ.setdefault("DOCUMENT_DB_PATH", "/data/rebuild-20260821/documents.db")
    # Avoid factory default; we construct stores explicitly.
    os.environ["VECTOR_STORE_BACKEND"] = "marqo"

    from pipeline.vector_store import MarqoStore
    from pipeline.vector_store_qdrant import QdrantStore

    marqo = MarqoStore(url=args.marqo_url)
    qdrant = QdrantStore(url=args.qdrant_url)

    print("=== consensus qrels ===")
    qrels = build_consensus_qrels(marqo, qdrant, args.marqo_index, args.qdrant_index)
    with open(args.qrels_out, "w", encoding="utf-8") as fh:
        for row in qrels:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    labeled = sum(1 for r in qrels if not r["junk_query"] and r["relevant"])
    print(f"wrote {args.qrels_out} useful_with_labels={labeled}")

    print("\n=== prod-path arms (run_search) ===")
    arms = {
        "marqo_hybrid": (marqo, args.marqo_index, "none"),
        "marqo_hybrid_bm25lite": (marqo, args.marqo_index, "bm25lite"),
        "qdrant_hybrid": (qdrant, args.qdrant_index, "none"),
        "qdrant_hybrid_bm25lite": (qdrant, args.qdrant_index, "bm25lite"),
    }
    arm_keys: dict[str, dict[str, list[str]]] = {a: {} for a in arms}
    latencies: dict[str, list[float]] = {a: [] for a in arms}
    for row in qrels:
        if row.get("junk_query") or not row.get("relevant"):
            continue
        qid, query = row["query_id"], row["query"]
        print(f"  query {qid}")
        for arm, (store, index, rerank) in arms.items():
            keys, ms = run_arm(store, index, query, rerank)
            arm_keys[arm][qid] = keys
            latencies[arm].append(ms)

    scored = score_rows(qrels, arm_keys)
    decision = decide(scored["summary"], scored["spnf_summary"])

    print("\nMEAN (useful queries)")
    for arm, s in scored["summary"].items():
        p50 = statistics.median(latencies[arm]) if latencies[arm] else 0
        print(
            f"  {arm:28s} R@5={s['recall@5']:.3f} R@10={s['recall@10']:.3f} "
            f"MRR={s['mrr']:.3f} nDCG@10={s['ndcg@10']:.3f} p50_ms={p50:.0f}"
        )
    print("\nSPNF subset R@10")
    for arm, s in scored["spnf_summary"].items():
        print(f"  {arm:28s} R@10={s['recall@10']:.3f} MRR={s['mrr']:.3f}")

    print("\nDECISION")
    print(json.dumps(decision, indent=2))

    out = {
        "qrels_path": args.qrels_out,
        "labeled_useful_queries": labeled,
        "summary": scored["summary"],
        "spnf_summary": scored["spnf_summary"],
        "per_query": scored["per_query"],
        "latency_p50_ms": {a: round(statistics.median(v), 1) if v else None for a, v in latencies.items()},
        "decision": decision,
        "changed_prod": False,
    }
    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(f"wrote {args.json_out}")
    return 0 if decision["pass_bar"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
