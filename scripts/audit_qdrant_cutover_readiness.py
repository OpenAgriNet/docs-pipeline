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


def collection_schema_from_rest(q_info: dict[str, Any]) -> dict[str, Any]:
    """Derive dense/sparse/IDF facts from a Qdrant GET /collections/{name} body."""
    result = q_info.get("result") or q_info
    params = ((result.get("config") or {}).get("params") or {})
    vectors = params.get("vectors") or {}
    sparse = params.get("sparse_vectors") or {}
    dense_cfg = vectors.get("dense") if isinstance(vectors, dict) else None
    bm25_cfg = sparse.get("bm25") if isinstance(sparse, dict) else None
    dense_size = (dense_cfg or {}).get("size") if isinstance(dense_cfg, dict) else None
    dense_distance = (dense_cfg or {}).get("distance") if isinstance(dense_cfg, dict) else None
    modifier = None
    if isinstance(bm25_cfg, dict):
        modifier = bm25_cfg.get("modifier")
    modifier_name = str(modifier).strip().lower() if modifier is not None else ""
    ok = (
        isinstance(dense_cfg, dict)
        and bool(dense_size)
        and str(dense_distance or "").lower() in {"cosine", "cos"}
        and isinstance(bm25_cfg, dict)
        and modifier_name == "idf"
    )
    return {
        "ok": bool(ok),
        "dense": dense_cfg,
        "bm25": bm25_cfg,
        "dense_size": dense_size,
        "dense_distance": dense_distance,
        "modifier": modifier,
        "modifier_name": modifier_name,
    }


def registered_tenant_indexes() -> list[dict[str, Any]]:
    """Every tenant_indexes row with the collection a Qdrant flip would query."""
    from pipeline import db as pipeline_db
    from pipeline.vector_store import resolve_backend_index

    rows = []
    for row in pipeline_db.list_all_tenant_indexes():
        stored = str(row.get("marqo_index") or "")
        resolved = resolve_backend_index(stored, backend="qdrant") or stored
        rows.append(
            {
                "instance": row.get("instance"),
                "name": row.get("name"),
                "marqo_index": stored,
                "resolved_qdrant": resolved,
                "is_default": row.get("is_default"),
                "status": row.get("status"),
            }
        )
    return rows


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

    q_info: dict[str, Any] = {}
    # 2. Qdrant healthy + collection
    try:
        with urllib.request.urlopen(f"{qdrant_url}/readyz", timeout=15) as resp:
            ready = resp.read().decode()
        q_info = http_json(f"{qdrant_url}/collections/{qdrant_index}")
        q_result = q_info.get("result") or {}
        q_pts = int(q_result.get("points_count") or 0)
        q_status = q_result.get("status")
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

    # 4. Resolved application index vs Qdrant collection
    stored_index = ""
    resolved_index = ""
    try:
        from pipeline.vector_store import (
            default_physical_index,
            qdrant_physical_default,
            resolve_backend_index,
            vector_store_backend,
        )
        from pipeline import db as pipeline_db

        stored_index = str(pipeline_db.get_search_settings().get("indexName") or "")
        resolved_index = (
            resolve_backend_index(stored_index) or default_physical_index()
        )
        flip_index = resolve_backend_index(stored_index, backend="qdrant") or qdrant_physical_default()
        env_qdrant = qdrant_physical_default()
        check(
            "resolved_application_index",
            bool(resolved_index) and bool(flip_index),
            (
                f"backend={vector_store_backend()!r} stored_search_index_name={stored_index!r} "
                f"live_resolved={resolved_index!r} after_qdrant_flip={flip_index!r} "
                f"QDRANT_INDEX_NAME={env_qdrant!r}"
            ),
            blockers,
            warns,
            notes,
        )
        target = qdrant_index or env_qdrant
        if target and flip_index != target:
            check(
                "cutover_index_resolution",
                False,
                (
                    f"a VECTOR_STORE_BACKEND=qdrant flip would query {flip_index!r}, "
                    f"not the populated collection {target!r}. Set QDRANT_INDEX_NAME "
                    "or QDRANT_INDEX_MAP."
                ),
                blockers,
                warns,
                notes,
            )
        else:
            check(
                "cutover_index_resolution",
                True,
                f"qdrant flip would query {flip_index!r}",
                blockers,
                warns,
                notes,
            )
    except Exception as exc:
        warn("resolved_application_index", f"could not resolve via DB/env: {exc}", warns, notes)

    # 5. Live dense / sparse / IDF schema on the audit collection
    try:
        schema = collection_schema_from_rest(q_info)
        check(
            "qdrant_passage_schema",
            bool(schema["ok"]),
            (
                f"dense={schema['dense']!r} bm25={schema['bm25']!r} "
                f"modifier={schema['modifier']!r}. "
                "A collection created before Modifier.IDF needs recreate + reingest."
            ),
            blockers,
            warns,
            notes,
        )
    except Exception as exc:
        check("qdrant_passage_schema", False, str(exc), blockers, warns, notes)

    # 5b. Every registered tenant collection exists with the same gate.
    # resolve_backend_index() suffix-translates names whether or not Qdrant
    # has the collection; a flip then fails at query time.
    try:
        tenant_rows = registered_tenant_indexes()
        if not tenant_rows:
            warn(
                "tenant_collections",
                "tenant_indexes is empty — unrestricted index is the only cutover target",
                warns,
                notes,
            )
        seen_resolved: set[str] = set()
        for row in tenant_rows:
            resolved = row["resolved_qdrant"]
            label = f"tenant_collection:{row['instance']}/{row['name']}"
            if not resolved:
                check(label, False, "resolved collection name is empty", blockers, warns, notes)
                continue
            if resolved in seen_resolved:
                check(
                    label,
                    True,
                    f"stored={row['marqo_index']!r} resolved={resolved!r} (already checked)",
                    blockers,
                    warns,
                    notes,
                )
                continue
            seen_resolved.add(resolved)
            try:
                info = http_json(f"{qdrant_url}/collections/{resolved}")
                schema = collection_schema_from_rest(info)
                pts = int((info.get("result") or {}).get("points_count") or 0)
                check(
                    label,
                    bool(schema["ok"]),
                    (
                        f"stored={row['marqo_index']!r} resolved={resolved!r} "
                        f"points={pts} dense_size={schema['dense_size']!r} "
                        f"distance={schema['dense_distance']!r} "
                        f"modifier={schema['modifier']!r}"
                    ),
                    blockers,
                    warns,
                    notes,
                )
            except Exception as exc:
                check(
                    label,
                    False,
                    (
                        f"stored={row['marqo_index']!r} resolved={resolved!r} "
                        f"not usable: {exc}"
                    ),
                    blockers,
                    warns,
                    notes,
                )
    except Exception as exc:
        check("tenant_collections", False, f"could not enumerate tenant_indexes: {exc}", blockers, warns, notes)

    # 6. Code factory switch + bm25lite (inspect the implementation, never a tautology)
    try:
        import inspect

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
        rerank_src = inspect.getsource(search_svc.rerank_hits)
        bm25_ok = (
            "bm25lite" in rerank_src
            and callable(getattr(search_svc, "bm25lite_scores", None))
        )
        check(
            "bm25lite_in_prod_path",
            bm25_ok,
            (
                "rerank_hits implements bm25lite via bm25lite_scores"
                if bm25_ok
                else "rerank_hits does not implement bm25lite"
            ),
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
    cutover_ready = ready and "cutover_index_resolution" not in blockers
    print("\n=== SUMMARY ===")
    print(f"blockers={blockers or 'none'}")
    print(f"warnings={warns}")
    print(f"infra_checks_pass={ready}")
    print(f"prod_cutover_ready={cutover_ready} (false until resolved index + IDF schema pass)")
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
