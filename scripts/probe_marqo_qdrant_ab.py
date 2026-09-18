#!/usr/bin/env python3
"""Scoped Marqo vs Qdrant A/B probe (#151).

Compares rebuild Marqo index vs shadow Qdrant collection on H100:
  1. Field / count parity
  2. Filter semantics (is_reference / query_enabled)
  3. Mode matrix (LEXICAL / TENSOR / HYBRID) overlap@10

Does not touch prod chat indexes. Example::

    VECTOR_STORE_BACKEND=qdrant \\
    QDRANT_URL=http://qdrant:6333 \\
    EMBEDDING_BACKEND=fastembed \\
    python scripts/probe_marqo_qdrant_ab.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

MARQO_URL = os.environ.get("MARQO_URL", "http://127.0.0.1:8882").rstrip("/")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
MARQO_INDEX = os.environ.get(
    "MARQO_AB_INDEX", "amul-veterinary-rebuild-20260821"
)
QDRANT_INDEX = os.environ.get(
    "QDRANT_AB_INDEX", "amul-veterinary-rebuild-20260821-qdrant"
)

CORE_FIELDS = {
    "workflow_id",
    "doc_id",
    "chunk_num",
    "filename",
    "text",
    "is_reference",
    "query_enabled",
    "domain_tags",
    "doc_language",
    "page_start",
    "page_end",
    "instance",
    "section",
    "source",
    "type",
}

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
    "instance",
    "section",
    "source",
    "type",
]

QUERIES = [
    ("lexical_known", "jeevamrut beejamrut ghanjeevamrut natural farming preparation"),
    ("farmer_spnf", "SPNF Palekar Devvrat natural farming methods"),
    ("farmer_gu_fmd", "ખરવા મોવાસા ગાય"),
    ("exact_en", "foot and mouth disease blisters mouth"),
    ("junk_prone", "subscriber notice printer magazine committee"),
]

MODES = ("LEXICAL", "TENSOR", "HYBRID")
FILTERS = (
    ("none", None),
    ("ref_false", "is_reference:false"),
    ("ref_true", "is_reference:true"),
)

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


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 120) -> Any:
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
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail[:600]}") from exc


def hit_key(hit: dict) -> str:
    wf = hit.get("workflow_id") or hit.get("doc_id") or "?"
    chunk = hit.get("chunk_num")
    if chunk is None:
        return f"{wf}|{hit.get('_id') or hit.get('filename') or '?'}"
    return f"{wf}|{chunk}"


def snippet(hit: dict, n: int = 100) -> str:
    return re.sub(r"\s+", " ", str(hit.get("text") or ""))[:n]


def is_interesting(hit: dict) -> bool:
    blob = " ".join(
        str(hit.get(k) or "")
        for k in ("filename", "name_en", "title_en", "text", "description")
    ).lower()
    return any(token in blob for token in INTERESTING)


def with_e5_prefix(query: str, method: str) -> str:
    if method.upper() in {"TENSOR", "HYBRID"} and not query.lower().startswith("query:"):
        return f"query: {query}"
    return query


def marqo_search(query: str, method: str, limit: int, filter_string: str | None) -> tuple[list[dict], float]:
    body: dict[str, Any] = {
        "q": with_e5_prefix(query, method),
        "limit": limit,
        "searchMethod": method.upper(),
        "attributesToRetrieve": ATTRS,
    }
    if filter_string:
        body["filter"] = filter_string
    if method.upper() in {"TENSOR", "HYBRID"}:
        body["efSearch"] = 256
    if method.upper() == "HYBRID":
        body["hybridParameters"] = {
            "alpha": 0.6,
            "rankingMethod": "rrf",
            "rrfK": 60,
            "searchableAttributesLexical": ["text", "description"],
            "searchableAttributesTensor": ["text_for_embedding"],
        }
    elif method.upper() == "TENSOR":
        body["searchableAttributes"] = ["text_for_embedding"]
    else:
        body["searchableAttributes"] = ["text", "description"]
    t0 = time.perf_counter()
    result = http_json("POST", f"{MARQO_URL}/indexes/{MARQO_INDEX}/search", body)
    ms = (time.perf_counter() - t0) * 1000
    return list(result.get("hits") or []), ms


def qdrant_store():
    os.environ.setdefault("VECTOR_STORE_BACKEND", "qdrant")
    os.environ.setdefault("EMBEDDING_BACKEND", "fastembed")
    os.environ.setdefault("EMBEDDING_NORMALIZE", "true")
    from pipeline.vector_store_qdrant import QdrantStore

    return QdrantStore(url=QDRANT_URL)


def qdrant_search(
    store, query: str, method: str, limit: int, filter_string: str | None
) -> tuple[list[dict], float]:
    request: dict[str, Any] = {
        "q": with_e5_prefix(query, method),
        "limit": limit,
        "search_method": method.lower(),
    }
    if filter_string:
        request["filter_string"] = filter_string
    if method.upper() == "HYBRID":
        request["hybrid_parameters"] = {"rrfK": 60, "alpha": 0.6}
    t0 = time.perf_counter()
    result = store.search(QDRANT_INDEX, **request)
    ms = (time.perf_counter() - t0) * 1000
    return list(result.get("hits") or []), ms


def overlap(a: list[dict], b: list[dict], k: int = 10) -> dict[str, Any]:
    ka = [hit_key(h) for h in a[:k]]
    kb = [hit_key(h) for h in b[:k]]
    sa, sb = set(ka), set(kb)
    inter = sa & sb
    return {
        "overlap": len(inter),
        "k": k,
        "jaccard": round(len(inter) / max(1, len(sa | sb)), 3),
        "marqo_only": sorted(sa - sb)[:5],
        "qdrant_only": sorted(sb - sa)[:5],
    }


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def check_counts_and_fields(store) -> dict[str, Any]:
    section("1. Counts + field parity")
    marqo_stats = http_json("GET", f"{MARQO_URL}/indexes/{MARQO_INDEX}/stats")
    q_info = http_json("GET", f"{QDRANT_URL}/collections/{QDRANT_INDEX}")
    q_result = (q_info or {}).get("result") or {}
    marqo_docs = int((marqo_stats or {}).get("numberOfDocuments") or 0)
    q_points = int(q_result.get("points_count") or 0)

    # Marqo settings field names (structured: top-level allFields)
    settings = http_json("GET", f"{MARQO_URL}/indexes/{MARQO_INDEX}/settings") or {}
    marqo_fields: set[str] = set()
    all_fields = settings.get("allFields")
    if not isinstance(all_fields, list):
        nested = settings.get("type")
        if isinstance(nested, dict):
            all_fields = (nested.get("structured") or {}).get("allFields")
    for entry in all_fields or []:
        name = entry.get("name") if isinstance(entry, dict) else None
        if name:
            marqo_fields.add(name)

    q_payload = set((q_result.get("payload_schema") or {}).keys())
    q_synth = set(store.field_names(QDRANT_INDEX))
    q_fields = q_payload | q_synth

    missing_on_q = sorted(CORE_FIELDS - q_fields)
    missing_on_m = sorted(CORE_FIELDS - marqo_fields) if marqo_fields else ["(marqo field list empty)"]
    shared_core = sorted(CORE_FIELDS & q_fields & (marqo_fields or CORE_FIELDS))

    print(f"marqo_index={MARQO_INDEX} docs={marqo_docs} vectors={(marqo_stats or {}).get('numberOfVectors')}")
    print(f"qdrant_collection={QDRANT_INDEX} points={q_points} indexed={q_result.get('indexed_vectors_count')}")
    print(f"count_delta={q_points - marqo_docs}")
    print(f"core_shared={shared_core}")
    print(f"core_missing_qdrant={missing_on_q}")
    print(f"core_missing_marqo={missing_on_m}")
    print(f"qdrant_payload_indexes={sorted(q_payload)}")
    ok = abs(q_points - marqo_docs) <= 5 and not missing_on_q
    print(f"PASS={ok}")
    return {
        "marqo_docs": marqo_docs,
        "qdrant_points": q_points,
        "missing_on_q": missing_on_q,
        "ok": ok,
    }


def qdrant_scroll_one(store, workflow_id: str, chunk_num: int) -> dict | None:
    from qdrant_client.http import models as rest

    points, _ = store.client().scroll(
        collection_name=QDRANT_INDEX,
        scroll_filter=rest.Filter(
            must=[
                rest.FieldCondition(key="workflow_id", match=rest.MatchValue(value=str(workflow_id))),
                rest.FieldCondition(key="chunk_num", match=rest.MatchValue(value=int(chunk_num))),
            ]
        ),
        limit=1,
        with_payload=True,
        with_vectors=False,
    )
    if not points:
        return None
    from pipeline.vector_store_qdrant import _hit_from_point

    return _hit_from_point(points[0])


def sample_payload_parity(store, n: int = 12) -> dict[str, Any]:
    section("1b. Sample payload parity (scroll)")
    hits, _ = marqo_search(
        "jeevamrut preparation", "HYBRID", limit=max(n, 20), filter_string="is_reference:false"
    )
    compared = 0
    mismatches: list[str] = []
    for hit in hits[:n]:
        wf = hit.get("workflow_id")
        chunk = hit.get("chunk_num")
        if wf is None or chunk is None:
            continue
        compared += 1
        try:
            match = qdrant_scroll_one(store, str(wf), int(chunk))
        except Exception as exc:
            mismatches.append(f"{hit_key(hit)}: scroll error {exc}")
            continue
        if match is None:
            mismatches.append(f"{hit_key(hit)}: missing on qdrant")
            continue
        for field in (
            "is_reference",
            "query_enabled",
            "doc_language",
            "filename",
            "page_start",
            "page_end",
        ):
            mv, qv = hit.get(field), match.get(field)
            if field in {"is_reference", "query_enabled"}:
                # Qdrant defaults query_enabled=True when Marqo omits the field.
                if field == "query_enabled" and mv is None and qv is True:
                    continue
                mv = bool(mv) if mv is not None else None
                qv = bool(qv) if qv is not None else None
            # Marqo often omits empty strings as null.
            if (mv is None or mv == "") and (qv is None or qv == ""):
                continue
            if mv != qv:
                mismatches.append(f"{hit_key(hit)}.{field}: marqo={mv!r} qdrant={qv!r}")
        mt = re.sub(r"\s+", " ", str(hit.get("text") or "")).strip()
        qt = re.sub(r"\s+", " ", str(match.get("text") or "")).strip()
        if mt[:200] != qt[:200]:
            mismatches.append(f"{hit_key(hit)}.text_prefix mismatch")
    print(f"compared={compared} mismatches={len(mismatches)}")
    for line in mismatches[:20]:
        print(f"  ! {line}")
    ok = compared > 0 and len(mismatches) == 0
    print(f"PASS={ok}")
    return {"compared": compared, "mismatches": mismatches, "ok": ok}


def filter_semantics(store, limit: int = 10) -> dict[str, Any]:
    section("2. Filter semantics")
    query = "Palekar jeevamrut natural farming"
    rows = []
    for fname, fstr in FILTERS:
        try:
            m_hits, m_ms = marqo_search(query, "HYBRID", limit, fstr)
        except Exception as exc:
            print(f"  marqo filter={fname} ERROR {exc}")
            m_hits, m_ms = [], -1.0
        try:
            q_hits, q_ms = qdrant_search(store, query, "HYBRID", limit, fstr)
        except Exception as exc:
            print(f"  qdrant filter={fname} ERROR {exc}")
            q_hits, q_ms = [], -1.0
        ov = overlap(m_hits, q_hits, k=limit)
        m_int = sum(1 for h in m_hits if is_interesting(h))
        q_int = sum(1 for h in q_hits if is_interesting(h))
        m_ref = sum(1 for h in m_hits if h.get("is_reference") is True)
        q_ref = sum(1 for h in q_hits if h.get("is_reference") is True)
        row = {
            "filter": fname,
            "marqo_n": len(m_hits),
            "qdrant_n": len(q_hits),
            "overlap@k": ov["overlap"],
            "jaccard": ov["jaccard"],
            "marqo_interesting": m_int,
            "qdrant_interesting": q_int,
            "marqo_ref_true_in_hits": m_ref,
            "qdrant_ref_true_in_hits": q_ref,
            "marqo_ms": round(m_ms, 1),
            "qdrant_ms": round(q_ms, 1),
        }
        rows.append(row)
        print(
            f"  {fname:12s} m={row['marqo_n']:2d} q={row['qdrant_n']:2d} "
            f"overlap={row['overlap@k']:2d} j={row['jaccard']:.2f} "
            f"int(m/q)={m_int}/{q_int} ref_true(m/q)={m_ref}/{q_ref} "
            f"ms(m/q)={row['marqo_ms']:.0f}/{row['qdrant_ms']:.0f}"
        )
        if fname == "ref_false":
            print(f"    marqo top:  {hit_key(m_hits[0]) if m_hits else '-'} | {snippet(m_hits[0]) if m_hits else ''}")
            print(f"    qdrant top: {hit_key(q_hits[0]) if q_hits else '-'} | {snippet(q_hits[0]) if q_hits else ''}")
    # Soft pass: ref_false should keep interesting hits on both; ref_true should be mostly refs on both
    ref_false = next(r for r in rows if r["filter"] == "ref_false")
    ref_true = next(r for r in rows if r["filter"] == "ref_true")
    ok = (
        ref_false["marqo_interesting"] > 0
        and ref_false["qdrant_interesting"] > 0
        and ref_true["marqo_ref_true_in_hits"] >= max(1, ref_true["marqo_n"] // 2)
        and ref_true["qdrant_ref_true_in_hits"] >= max(1, ref_true["qdrant_n"] // 2)
    )
    print(f"PASS={ok} (interesting under ref_false + mostly refs under ref_true)")
    return {"rows": rows, "ok": ok}


def mode_matrix(store, limit: int = 10) -> dict[str, Any]:
    section("3. Mode matrix (overlap@10)")
    rows = []
    for qid, query in QUERIES:
        for mode in MODES:
            try:
                m_hits, m_ms = marqo_search(query, mode, limit, "is_reference:false")
            except Exception as exc:
                print(f"  {qid}/{mode} marqo ERROR {exc}")
                m_hits, m_ms = [], -1.0
            try:
                q_hits, q_ms = qdrant_search(store, query, mode, limit, "is_reference:false")
            except Exception as exc:
                print(f"  {qid}/{mode} qdrant ERROR {exc}")
                q_hits, q_ms = [], -1.0
            ov = overlap(m_hits, q_hits, k=limit)
            m_int = sum(1 for h in m_hits if is_interesting(h))
            q_int = sum(1 for h in q_hits if is_interesting(h))
            row = {
                "query": qid,
                "mode": mode,
                "overlap": ov["overlap"],
                "jaccard": ov["jaccard"],
                "marqo_interesting": m_int,
                "qdrant_interesting": q_int,
                "marqo_ms": round(m_ms, 1),
                "qdrant_ms": round(q_ms, 1),
                "marqo_only": ov["marqo_only"],
                "qdrant_only": ov["qdrant_only"],
            }
            rows.append(row)
            print(
                f"  {qid:14s} {mode:7s} overlap={ov['overlap']:2d}/10 j={ov['jaccard']:.2f} "
                f"int(m/q)={m_int}/{q_int} ms={row['marqo_ms']:.0f}/{row['qdrant_ms']:.0f}"
            )
    # Soft pass: known lexical + hybrid farmer should have some interesting on both
    focus = [
        r
        for r in rows
        if r["query"] in {"lexical_known", "farmer_spnf"} and r["mode"] in {"LEXICAL", "HYBRID"}
    ]
    ok = all(r["marqo_interesting"] > 0 and r["qdrant_interesting"] > 0 for r in focus) and focus
    print(f"PASS={bool(ok)} (interesting hits on lexical_known + farmer_spnf for LEXICAL/HYBRID)")
    return {"rows": rows, "ok": bool(ok)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--sample", type=int, default=12)
    parser.add_argument("--json-out", default="", help="Optional path for machine-readable summary")
    args = parser.parse_args()

    print(f"Marqo  {MARQO_URL}  index={MARQO_INDEX}")
    print(f"Qdrant {QDRANT_URL}  collection={QDRANT_INDEX}")

    store = qdrant_store()
    # Warm embedder once
    print("warming qdrant embedder...")
    qdrant_search(store, "warmup", "TENSOR", 1, None)

    summary = {
        "counts": check_counts_and_fields(store),
        "samples": sample_payload_parity(store, n=args.sample),
        "filters": filter_semantics(store, limit=args.limit),
        "modes": mode_matrix(store, limit=args.limit),
    }
    section("SUMMARY")
    overall = all(summary[k]["ok"] for k in ("counts", "samples", "filters", "modes"))
    for key in ("counts", "samples", "filters", "modes"):
        print(f"  {key}: PASS={summary[key]['ok']}")
    print(f"OVERALL_PASS={overall}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)
        print(f"wrote {args.json_out}")

    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
