#!/usr/bin/env python3
"""Marqo vs Qdrant eval suites #4 / #5 / #6 (#151).

4) Labeled retrieval quality (Recall@k / MRR / nDCG@10) vs qrels
5) Junk-query smoke (interesting vs masthead-ish rate)
6) Ops smoke on Qdrant only (purge workflow → count drop → reingest → restore)

Arms:
  - marqo_hybrid
  - qdrant_hybrid
  - qdrant_hybrid_rerank  (bm25lite post-rerank, same as issue #150 probe)

Does not flip prod. Keep Marqo indexes read-only; Qdrant purge uses one workflow.
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
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

MARQO_URL = os.environ.get("MARQO_URL", "http://127.0.0.1:8882").rstrip("/")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
MARQO_INDEX = os.environ.get("MARQO_AB_INDEX", "amul-veterinary-rebuild-20260821")
QDRANT_INDEX = os.environ.get(
    "QDRANT_AB_INDEX", "amul-veterinary-rebuild-20260821-qdrant"
)
DEFAULT_QRELS = os.environ.get("QRELS_PATH", "/tmp/qrels_seed.jsonl")

ATTRS = [
    "filename",
    "name_en",
    "title_en",
    "doc_id",
    "workflow_id",
    "text",
    "description",
    "is_reference",
    "chunk_num",
    "domain_tags",
    "doc_language",
    "page_start",
    "page_end",
]

INTERESTING = (
    "krushigovidya",
    "palekar",
    "devvrat",
    "spnf",
    "beejamrut",
    "jeevamrut",
    "ghanjeevamrut",
    "zero budget",
    "natural farming",
)
JUNKISH = (
    "subscriber",
    "printer",
    "editorial board",
    "table of contents",
    "committee",
    "advertisement",
    "price list",
    "subscription",
    "masthead",
)

_TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 180) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail[:700]}") from exc


def hit_key(hit: dict) -> str:
    wf = hit.get("workflow_id") or hit.get("doc_id") or "?"
    return f"{wf}|{hit.get('chunk_num')}"


def with_e5(query: str) -> str:
    if not query.lower().startswith("query:"):
        return f"query: {query}"
    return query


def marqo_search(query: str, limit: int, filter_string: str | None = "is_reference:false") -> tuple[list[dict], float]:
    body: dict[str, Any] = {
        "q": with_e5(query),
        "limit": limit,
        "searchMethod": "HYBRID",
        "efSearch": 256,
        "attributesToRetrieve": ATTRS,
        "hybridParameters": {
            "alpha": 0.6,
            "rankingMethod": "rrf",
            "rrfK": 60,
            "searchableAttributesLexical": ["text", "description"],
            "searchableAttributesTensor": ["text_for_embedding"],
        },
    }
    if filter_string:
        body["filter"] = filter_string
    t0 = time.perf_counter()
    result = http_json("POST", f"{MARQO_URL}/indexes/{MARQO_INDEX}/search", body)
    return list(result.get("hits") or []), (time.perf_counter() - t0) * 1000


def qdrant_store():
    os.environ.setdefault("VECTOR_STORE_BACKEND", "qdrant")
    os.environ.setdefault("EMBEDDING_BACKEND", "fastembed")
    os.environ.setdefault("EMBEDDING_NORMALIZE", "true")
    from pipeline.vector_store_qdrant import QdrantStore

    return QdrantStore(url=QDRANT_URL)


def qdrant_search(
    store, query: str, limit: int, filter_string: str | None = "is_reference:false"
) -> tuple[list[dict], float]:
    request: dict[str, Any] = {
        "q": with_e5(query),
        "limit": limit,
        "search_method": "hybrid",
        "hybrid_parameters": {"rrfK": 60, "alpha": 0.6},
    }
    if filter_string:
        request["filter_string"] = filter_string
    t0 = time.perf_counter()
    result = store.search(QDRANT_INDEX, **request)
    return list(result.get("hits") or []), (time.perf_counter() - t0) * 1000


def tokenize(value: str) -> list[str]:
    return _TOKEN_RE.findall(re.sub(r"\s+", " ", (value or "").strip().lower()))


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


def rerank_bm25lite(query: str, hits: list[dict]) -> list[dict]:
    if not hits:
        return hits
    raw_scores = [float(hit.get("_score", hit.get("score", 0.0)) or 0.0) for hit in hits]
    minimum, maximum = min(raw_scores), max(raw_scores)
    denominator = (maximum - minimum) if maximum > minimum else 1.0
    semantic = [(s - minimum) / denominator for s in raw_scores]
    documents = [
        f"{hit.get('text') or ''} {hit.get('filename') or ''} {hit.get('description') or ''}".strip()
        for hit in hits
    ]
    bm25 = bm25lite_scores(query, documents)
    bmin, bmax = min(bm25), max(bm25)
    bden = (bmax - bmin) if bmax > bmin else 1.0
    nb = [(s - bmin) / bden for s in bm25]
    out = []
    for hit, sem, b in zip(hits, semantic, nb):
        enriched = dict(hit)
        enriched["_rerank_score"] = (
            0.50 * sem
            + 0.40 * b
            + (-0.10 if bool(hit.get("is_reference", False)) else 0.0)
        )
        out.append(enriched)
    out.sort(key=lambda h: float(h.get("_rerank_score", 0.0)), reverse=True)
    return out


def load_qrels(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def grades_map(row: dict) -> dict[str, int]:
    return {item["key"]: int(item.get("grade") or 1) for item in row.get("relevant") or [] if item.get("key")}


def recall_at(ranked_keys: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(ranked_keys[:k]) & relevant) / len(relevant)


def mrr(ranked_keys: list[str], relevant: set[str]) -> float:
    for i, key in enumerate(ranked_keys, start=1):
        if key in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at(ranked_keys: list[str], grades: dict[str, int], k: int) -> float:
    if not grades:
        return 0.0

    def dcg(keys: list[str]) -> float:
        total = 0.0
        for i, key in enumerate(keys[:k], start=1):
            rel = grades.get(key, 0)
            if rel:
                total += (2**rel - 1) / math.log2(i + 1)
        return total

    ideal = sorted(grades.values(), reverse=True)
    idcg = 0.0
    for i, rel in enumerate(ideal[:k], start=1):
        idcg += (2**rel - 1) / math.log2(i + 1)
    if idcg <= 0:
        return 0.0
    return dcg(ranked_keys) / idcg


def mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def is_interesting(hit: dict) -> bool:
    blob = " ".join(str(hit.get(k) or "") for k in ("filename", "text", "description")).lower()
    return any(tok in blob for tok in INTERESTING)


def is_junkish(hit: dict) -> bool:
    blob = " ".join(str(hit.get(k) or "") for k in ("filename", "text", "description")).lower()
    return any(tok in blob for tok in JUNKISH)


def suite4(store, qrels: list[dict], limit: int, candidate_cap: int) -> dict[str, Any]:
    section("4. Labeled retrieval quality (HYBRID + is_reference:false)")
    arms = ("marqo_hybrid", "qdrant_hybrid", "qdrant_hybrid_rerank")
    metrics: dict[str, dict[str, list[float]]] = {
        arm: {"recall@5": [], "recall@10": [], "mrr": [], "ndcg@10": [], "latency_ms": []}
        for arm in arms
    }
    per_query: list[dict] = []

    usable = [r for r in qrels if not r.get("junk_query") and grades_map(r)]
    print(f"labeled_queries={len(usable)} (junk queries excluded from #4)")

    for row in usable:
        qid, query = row["query_id"], row["query"]
        grades = grades_map(row)
        relevant = set(grades)
        m_hits, m_ms = marqo_search(query, limit=candidate_cap)
        q_hits, q_ms = qdrant_search(store, query, limit=candidate_cap)
        q_rerank = rerank_bm25lite(query, q_hits)[:limit]
        m_keys = [hit_key(h) for h in m_hits[:limit]]
        q_keys = [hit_key(h) for h in q_hits[:limit]]
        qr_keys = [hit_key(h) for h in q_rerank[:limit]]

        for arm, keys, ms in (
            ("marqo_hybrid", m_keys, m_ms),
            ("qdrant_hybrid", q_keys, q_ms),
            ("qdrant_hybrid_rerank", qr_keys, q_ms),
        ):
            metrics[arm]["recall@5"].append(recall_at(keys, relevant, 5))
            metrics[arm]["recall@10"].append(recall_at(keys, relevant, 10))
            metrics[arm]["mrr"].append(mrr(keys, relevant))
            metrics[arm]["ndcg@10"].append(ndcg_at(keys, grades, 10))
            metrics[arm]["latency_ms"].append(ms)

        pq = {
            "query_id": qid,
            "n_relevant": len(relevant),
            "marqo": {
                "recall@10": recall_at(m_keys, relevant, 10),
                "mrr": mrr(m_keys, relevant),
                "ndcg@10": ndcg_at(m_keys, grades, 10),
            },
            "qdrant": {
                "recall@10": recall_at(q_keys, relevant, 10),
                "mrr": mrr(q_keys, relevant),
                "ndcg@10": ndcg_at(q_keys, grades, 10),
            },
            "qdrant_rerank": {
                "recall@10": recall_at(qr_keys, relevant, 10),
                "mrr": mrr(qr_keys, relevant),
                "ndcg@10": ndcg_at(qr_keys, grades, 10),
            },
        }
        per_query.append(pq)
        print(
            f"  {qid:18s} R@10 m/q/qr="
            f"{pq['marqo']['recall@10']:.2f}/"
            f"{pq['qdrant']['recall@10']:.2f}/"
            f"{pq['qdrant_rerank']['recall@10']:.2f}  "
            f"MRR={pq['marqo']['mrr']:.2f}/{pq['qdrant']['mrr']:.2f}/{pq['qdrant_rerank']['mrr']:.2f}"
        )

    summary = {}
    print("\nMEAN METRICS")
    for arm in arms:
        summary[arm] = {k: round(mean(v), 4) for k, v in metrics[arm].items()}
        s = summary[arm]
        print(
            f"  {arm:22s} R@5={s['recall@5']:.3f} R@10={s['recall@10']:.3f} "
            f"MRR={s['mrr']:.3f} nDCG@10={s['ndcg@10']:.3f} "
            f"p50_ms={statistics.median(metrics[arm]['latency_ms']):.0f}"
        )

    # Decision bar: Qdrant (best of raw/rerank) within 0.05 R@10 of Marqo
    m_r = summary["marqo_hybrid"]["recall@10"]
    q_best_arm = max(
        ("qdrant_hybrid", "qdrant_hybrid_rerank"),
        key=lambda a: (summary[a]["recall@10"], summary[a]["ndcg@10"], summary[a]["mrr"]),
    )
    q_r = summary[q_best_arm]["recall@10"]
    delta = q_r - m_r
    ok = delta >= -0.05
    print(
        f"\nDECISION: best_qdrant_arm={q_best_arm} "
        f"R@10_delta_vs_marqo={delta:+.3f} PASS={ok} "
        f"(pass if Qdrant R@10 >= Marqo - 0.05)"
    )
    return {
        "summary": summary,
        "per_query": per_query,
        "best_qdrant_arm": q_best_arm,
        "delta_recall@10": delta,
        "ok": ok,
    }


def suite5(store, qrels: list[dict], limit: int = 10) -> dict[str, Any]:
    section("5. Junk / searchability smoke")
    junk_rows = [r for r in qrels if r.get("junk_query")]
    if not junk_rows:
        junk_rows = [
            {"query_id": "junk_subscriber", "query": "subscriber notice printer magazine committee"},
            {"query_id": "junk_toc", "query": "table of contents editorial board index page"},
        ]
    useful_rows = [
        r
        for r in qrels
        if r.get("query_id") in {"spnf_jeevamrut", "spnf_farmer", "zero_budget", "fmd_en"}
    ]
    rows_out = []
    for row in junk_rows + useful_rows:
        qid, query = row["query_id"], row["query"]
        m_hits, _ = marqo_search(query, limit=limit)
        q_hits, _ = qdrant_search(store, query, limit=limit)
        rec = {
            "query_id": qid,
            "junk_query": bool(row.get("junk_query")),
            "marqo_hits": len(m_hits),
            "qdrant_hits": len(q_hits),
            "marqo_interesting": sum(1 for h in m_hits if is_interesting(h)),
            "qdrant_interesting": sum(1 for h in q_hits if is_interesting(h)),
            "marqo_junkish": sum(1 for h in m_hits if is_junkish(h)),
            "qdrant_junkish": sum(1 for h in q_hits if is_junkish(h)),
        }
        rows_out.append(rec)
        print(
            f"  {qid:18s} hits(m/q)={rec['marqo_hits']}/{rec['qdrant_hits']} "
            f"int(m/q)={rec['marqo_interesting']}/{rec['qdrant_interesting']} "
            f"junkish(m/q)={rec['marqo_junkish']}/{rec['qdrant_junkish']}"
        )
    # Soft pass: both backends return hits for useful queries; SPNF set keeps interesting on Qdrant
    useful = [r for r in rows_out if not r["junk_query"]]
    spnf = [r for r in useful if r["query_id"].startswith("spnf") or r["query_id"] == "zero_budget"]
    ok = (
        all(r["marqo_hits"] > 0 and r["qdrant_hits"] > 0 for r in useful)
        and all(r["qdrant_interesting"] > 0 for r in spnf)
    )
    print(f"PASS={ok} (useful queries return hits; SPNF/zero_budget interesting on Qdrant)")
    return {"rows": rows_out, "ok": ok}


def qdrant_points() -> int:
    info = http_json("GET", f"{QDRANT_URL}/collections/{QDRANT_INDEX}")
    return int(((info or {}).get("result") or {}).get("points_count") or 0)


def suite6(store) -> dict[str, Any]:
    section("6. Ops smoke (Qdrant purge → reingest, latency)")
    from qdrant_client.http import models as rest
    from pipeline.vector_store_qdrant import _hit_from_point

    # Pick a workflow with a modest number of chunks via scroll sample.
    sample, _ = store.client().scroll(
        collection_name=QDRANT_INDEX,
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    if not sample:
        print("PASS=False (empty collection)")
        return {"ok": False, "error": "empty"}
    payload0 = sample[0].payload or {}
    workflow_id = str(payload0.get("workflow_id") or "")
    doc_id = str(payload0.get("doc_id") or workflow_id)
    if not workflow_id:
        print("PASS=False (sample missing workflow_id)")
        return {"ok": False, "error": "no_workflow"}

    # Collect all points for workflow (payload + vectors) for restore.
    points: list[Any] = []
    next_offset = None
    while True:
        batch, next_offset = store.client().scroll(
            collection_name=QDRANT_INDEX,
            scroll_filter=rest.Filter(
                must=[rest.FieldCondition(key="workflow_id", match=rest.MatchValue(value=workflow_id))]
            ),
            limit=64,
            offset=next_offset,
            with_payload=True,
            with_vectors=True,
        )
        points.extend(batch)
        if next_offset is None:
            break

    before = qdrant_points()
    n = len(points)
    print(f"target workflow_id={workflow_id} doc_id={doc_id} points={n} collection_before={before}")
    if n == 0 or n > 800:
        print(f"PASS=False (unexpected point count {n})")
        return {"ok": False, "error": f"bad_n={n}", "workflow_id": workflow_id}

    # Latency sample before mutation
    latencies = []
    for _ in range(8):
        _, ms = qdrant_search(store, "cattle fever treatment", limit=10)
        latencies.append(ms)
    print(f"latency_ms p50={statistics.median(latencies):.0f} p95={sorted(latencies)[max(0, int(0.95*len(latencies))-1)]:.0f}")

    # Purge by doc_id + workflow scope (same rules as Marqo purge)
    purge_result = store.delete_document(
        document_id=doc_id, index=QDRANT_INDEX, workflow_id=workflow_id
    )
    print(f"purge_result={purge_result}")
    after_delete = qdrant_points()
    remaining, _ = store.client().scroll(
        collection_name=QDRANT_INDEX,
        scroll_filter=rest.Filter(
            must=[rest.FieldCondition(key="workflow_id", match=rest.MatchValue(value=workflow_id))]
        ),
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    print(f"after_delete collection={after_delete} delta={before - after_delete} remaining_for_wf={len(remaining)}")

    # Restore via upsert of saved points (ops round-trip; avoids full SQLite re-embed)
    structs = [
        rest.PointStruct(id=p.id, vector=p.vector, payload=p.payload)
        for p in points
    ]
    store.client().upsert(collection_name=QDRANT_INDEX, points=structs, wait=True)
    after_restore = qdrant_points()
    restored, _ = store.client().scroll(
        collection_name=QDRANT_INDEX,
        scroll_filter=rest.Filter(
            must=[rest.FieldCondition(key="workflow_id", match=rest.MatchValue(value=workflow_id))]
        ),
        limit=n + 5,
        with_payload=True,
        with_vectors=False,
    )
    print(f"after_restore collection={after_restore} restored_points={len(restored)}")

    ok = (
        int(purge_result.get("deleted") or 0) == n
        and after_delete == before - n
        and len(remaining) == 0
        and after_restore == before
        and len(restored) == n
    )
    print(f"PASS={ok}")
    _ = _hit_from_point
    return {
        "ok": ok,
        "workflow_id": workflow_id,
        "doc_id": doc_id,
        "points": n,
        "before": before,
        "after_delete": after_delete,
        "after_restore": after_restore,
        "purge_result": purge_result,
        "latency_ms": {
            "p50": round(statistics.median(latencies), 1),
            "p95": round(sorted(latencies)[max(0, int(0.95 * len(latencies)) - 1)], 1),
            "samples": [round(x, 1) for x in latencies],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qrels", default=DEFAULT_QRELS)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--candidate-cap", type=int, default=30)
    parser.add_argument("--skip-ops", action="store_true")
    parser.add_argument("--skip-quality", action="store_true")
    parser.add_argument("--skip-junk", action="store_true")
    parser.add_argument("--json-out", default="/tmp/probe_marqo_qdrant_456.json")
    args = parser.parse_args()

    print(f"Marqo  {MARQO_URL} index={MARQO_INDEX}")
    print(f"Qdrant {QDRANT_URL} collection={QDRANT_INDEX}")
    print(f"qrels  {args.qrels}")

    qrels = load_qrels(args.qrels) if not (args.skip_quality and args.skip_junk) else []
    store = qdrant_store()
    print("warming embedder...")
    qdrant_search(store, "warmup", limit=1, filter_string=None)

    out: dict[str, Any] = {}
    if args.skip_quality:
        out["suite4"] = {"ok": True, "skipped": True}
    else:
        out["suite4"] = suite4(store, qrels, limit=args.limit, candidate_cap=args.candidate_cap)
    if args.skip_junk:
        out["suite5"] = {"ok": True, "skipped": True}
    else:
        if not qrels:
            qrels = load_qrels(args.qrels)
        out["suite5"] = suite5(store, qrels, limit=args.limit)
    if args.skip_ops:
        out["suite6"] = {"ok": True, "skipped": True}
    else:
        out["suite6"] = suite6(store)

    section("SUMMARY 4/5/6")
    overall = all(out[k]["ok"] for k in ("suite4", "suite5", "suite6"))
    for k in ("suite4", "suite5", "suite6"):
        print(f"  {k}: PASS={out[k]['ok']}")
    if "best_qdrant_arm" in out["suite4"]:
        print(
            f"  best_qdrant_arm={out['suite4']['best_qdrant_arm']} "
            f"delta_R@10={out['suite4']['delta_recall@10']:+.3f}"
        )
    print(f"OVERALL_PASS={overall}")

    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(f"wrote {args.json_out}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
