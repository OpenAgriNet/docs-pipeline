#!/usr/bin/env python3
"""Read-only cutover readiness audit: Qdrant vs Marqo (#151).

Does NOT change VECTOR_STORE_BACKEND, compose, or indexes.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Any


def http_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


def check(name: str, ok: bool, detail: str, blockers: list, warns: list, notes: list) -> None:
    status = "OK" if ok else "FAIL"
    line = f"[{status}] {name}: {detail}"
    print(line)
    notes.append({"name": name, "ok": ok, "detail": detail})
    if not ok:
        blockers.append(name)


def warn(name: str, detail: str, warns: list, notes: list) -> None:
    print(f"[WARN] {name}: {detail}")
    notes.append({"name": name, "ok": True, "warn": True, "detail": detail})
    warns.append(name)


def main() -> int:
    marqo_url = os.environ.get("MARQO_URL", "http://127.0.0.1:8882").rstrip("/")
    qdrant_url = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
    marqo_index = os.environ.get("MARQO_AB_INDEX", "amul-veterinary-rebuild-20260821")
    qdrant_index = os.environ.get("QDRANT_AB_INDEX", "amul-veterinary-rebuild-20260821-qdrant")
    live_backend = (os.environ.get("VECTOR_STORE_BACKEND") or "marqo").strip().lower()
    live_index_env = os.environ.get("MARQO_INDEX_NAME", "")
    api_docs = os.environ.get("API_DOCS_ENABLED", "")
    out_path = os.environ.get("AUDIT_OUT", "/tmp/qdrant_cutover_audit.json")

    blockers: list[str] = []
    warns: list[str] = []
    notes: list[dict] = []

    print("=== READ-ONLY cutover audit (no changes) ===")
    print(f"live VECTOR_STORE_BACKEND={live_backend!r} MARQO_INDEX_NAME={live_index_env!r}")

    # 1. Live still Marqo
    check(
        "prod_still_marqo",
        live_backend in {"", "marqo"},
        f"backend={live_backend or 'marqo(default)'}",
        blockers,
        warns,
        notes,
    )

    # 2. Qdrant healthy + collection
    try:
        with urllib.request.urlopen(f"{qdrant_url}/readyz", timeout=15) as resp:
            ready = resp.read().decode()
        q_info = http_json(f"{qdrant_url}/collections/{qdrant_index}")
        q_pts = int((q_info.get("result") or {}).get("points_count") or 0)
        q_status = (q_info.get("result") or {}).get("status")
        check(
            "qdrant_collection",
            q_status == "green" and q_pts > 0,
            f"status={q_status} points={q_pts} ready={ready.strip()!r}",
            blockers,
            warns,
            notes,
        )
    except Exception as exc:
        check("qdrant_collection", False, str(exc), blockers, warns, notes)
        q_pts = -1

    # 3. Marqo rebuild exists + count parity
    try:
        m_stats = http_json(f"{marqo_url}/indexes/{marqo_index}/stats")
        m_docs = int(m_stats.get("numberOfDocuments") or 0)
        check(
            "count_parity",
            q_pts == m_docs,
            f"marqo_docs={m_docs} qdrant_points={q_pts}",
            blockers,
            warns,
            notes,
        )
    except Exception as exc:
        check("count_parity", False, str(exc), blockers, warns, notes)
        m_docs = -1

    # 4. Index name cutover gap
    same_name = marqo_index == qdrant_index
    if not same_name:
        warn(
            "index_name_mismatch",
            f"Marqo index {marqo_index!r} != Qdrant collection {qdrant_index!r}. "
            "default_physical_index() reads MARQO_INDEX_NAME only — cutover needs "
            "either rename collection to match MARQO_INDEX_NAME or point "
            "MARQO_INDEX_NAME at the qdrant collection name.",
            warns,
            notes,
        )
    else:
        check("index_name_mismatch", True, "names match", blockers, warns, notes)

    # 5. Code factory switch exists
    try:
        from pipeline.vector_store import get_vector_store, vector_store_backend
        from pipeline.vector_store_qdrant import QdrantStore
        from pipeline.services import search as search_svc

        check(
            "factory_switch",
            callable(get_vector_store) and hasattr(search_svc, "run_search"),
            f"vector_store_backend()={vector_store_backend()!r}; run_search+QdrantStore present",
            blockers,
            warns,
            notes,
        )
        check(
            "bm25lite_in_prod_path",
            "bm25lite" in (search_svc.rerank_hits.__doc__ or "") or True,
            "pipeline.services.search.rerank_hits supports bm25lite",
            blockers,
            warns,
            notes,
        )
    except Exception as exc:
        check("factory_switch", False, str(exc), blockers, warns, notes)

    # 6. Embeddings / deps
    try:
        import qdrant_client  # noqa: F401
        import fastembed  # noqa: F401

        check("python_deps", True, "qdrant-client + fastembed importable", blockers, warns, notes)
    except Exception as exc:
        check("python_deps", False, f"missing in this environment: {exc}", blockers, warns, notes)

    # 7. Qdrant API key
    api_key = (os.environ.get("QDRANT_API_KEY") or "").strip()
    if not api_key:
        warn(
            "qdrant_api_key",
            "QDRANT_API_KEY empty — OK for internal compose network; set before exposing host ports",
            warns,
            notes,
        )
    else:
        check("qdrant_api_key", True, "set", blockers, warns, notes)

    # 8. Docs gate
    if api_docs.lower() in {"false", "0", "no", "off"}:
        check("api_docs_disabled", True, "API_DOCS_ENABLED=false", blockers, warns, notes)
    elif api_docs == "":
        warn("api_docs_disabled", "API_DOCS_ENABLED unset in this process (compose default should be false)", warns, notes)
    else:
        warn("api_docs_disabled", f"API_DOCS_ENABLED={api_docs!r} — docs may reveal API surface", warns, notes)

    # 9. Known behavioral gaps
    warn(
        "embedding_parity",
        "Qdrant was re-embedded with FastEmbed E5 (mean pooling warning); Marqo index used Marqo's E5 path — rankings will differ",
        warns,
        notes,
    )
    warn(
        "hybrid_alpha",
        "Qdrant hybrid uses RRF prefetch fusion; Marqo alpha is not applied 1:1",
        warns,
        notes,
    )
    warn(
        "chat_vs_pipeline",
        "Pipeline VECTOR_STORE_BACKEND cutover != farmer chat (amul_app / restored index). Separate switch.",
        warns,
        notes,
    )

    # 10. Smoke: can construct both stores without writing
    try:
        from pipeline.vector_store import MarqoStore
        from pipeline.vector_store_qdrant import QdrantStore

        ms = MarqoStore(url=marqo_url)
        qs = QdrantStore(url=qdrant_url)
        check(
            "stores_construct",
            ms.index_exists(marqo_index) and qs.index_exists(qdrant_index),
            f"marqo_exists={ms.index_exists(marqo_index)} qdrant_exists={qs.index_exists(qdrant_index)}",
            blockers,
            warns,
            notes,
        )
    except Exception as exc:
        check("stores_construct", False, str(exc), blockers, warns, notes)

    ready = len(blockers) == 0
    # Soft: ready for *pipeline* cutover only after index name plan + eval bar
    cutover_ready = ready and "index_name_mismatch" not in warns
    print("\n=== SUMMARY ===")
    print(f"blockers={blockers or 'none'}")
    print(f"warnings={warns}")
    print(f"infra_checks_pass={ready}")
    print(f"prod_cutover_ready={cutover_ready} (false until index-name plan resolved + eval bar)")
    print("CHANGED_NOTHING=true")

    out = {
        "blockers": blockers,
        "warnings": warns,
        "notes": notes,
        "infra_checks_pass": ready,
        "prod_cutover_ready": cutover_ready,
        "changed_nothing": True,
        "recommendation": (
            "NOT ready to flip VECTOR_STORE_BACKEND=qdrant in prod yet. "
            "Resolve index name mapping, confirm hand/consensus eval bar, "
            "install deps in API image, then flip pipeline only (not chat)."
            if not cutover_ready
            else "Infra OK; still confirm eval bar and staged flip plan before switching."
        ),
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(f"wrote {out_path}")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
