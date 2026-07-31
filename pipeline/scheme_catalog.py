"""
Master Scheme Catalog — source of truth for AI tool prompts and scheme lists.

Owned by docs-pipeline. Consumers (bharat-oan-api, bharat-provider-backend)
poll GET /catalog/v1/snapshot and cache the result.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from . import db

SCHEME_CODE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Prompt "vector-indexed" searchable set (13). Exclude nbm (legacy-only).
BOOTSTRAP_VECTOR_SCHEMES: dict[str, dict[str, Any]] = {
    "cdp": {
        "scheme_name": "Crop Diversification Programme",
        "scheme_aliases": [
            "CDP",
            "crop diversification",
            "crop diversification programme",
            "crop diversification program",
        ],
    },
    "cotton-mission": {
        "scheme_name": "Cotton Mission",
        "scheme_aliases": ["cotton mission", "cotton scheme", "nfsm cotton", "NFSM Cotton"],
    },
    "e-nam": {
        "scheme_name": "Electronic National Agriculture Market",
        "scheme_aliases": [
            "e-NAM",
            "eNAM",
            "enam",
            "national agriculture market",
            "electronic nam",
            "electronic national agriculture market",
        ],
    },
    "makhana": {
        "scheme_name": "Central Sector Scheme for Development of Makhana",
        "scheme_aliases": [
            "makhana",
            "makana",
            "foxnut",
            "horticulture makhana",
            "makhana scheme",
            "development of makhana",
        ],
    },
    "midh": {
        "scheme_name": "Mission for Integrated Development of Horticulture",
        "scheme_aliases": [
            "MIDH",
            "horticulture mission",
            "krishonnati horticulture",
            "national horticulture mission",
            "integrated development of horticulture",
        ],
    },
    "mif": {
        "scheme_name": "Micro Irrigation Fund",
        "scheme_aliases": ["MIF", "micro irrigation fund", "Micro Irrigation Fund scheme"],
    },
    "nmeo": {
        "scheme_name": "National Mission on Edible Oils – Oilseeds",
        "scheme_aliases": [
            "NMEO",
            "NMEO-OS",
            "NMEO OS",
            "oilseeds mission",
            "nmoe",
            "national mission on edible oils",
            "edible oils oilseeds",
        ],
    },
    "pkvy": {
        "scheme_name": "Paramparagat Krishi Vikas Yojana",
        "scheme_aliases": [
            "PKVY",
            "paramparagat krishi vikas yojana",
            "paramparagat krishi",
            "organic farming scheme",
        ],
    },
    "pm-ddky": {
        "scheme_name": "Prime Minister Dhan–Dhaanya Krishi Yojana",
        "scheme_aliases": [
            "PM-DDKY",
            "PM DDKY",
            "pm ddky",
            "dhan-dhaanya krishi yojana",
            "dhan dhaanya krishi yojana",
            "dhan-dhaanya",
            "dhan dhaanya",
            "PM Dhan Dhaanya Krishi Yojana",
        ],
    },
    "pm-kmy": {
        "scheme_name": "Pradhan Mantri Kisan Maandhan Yojana",
        "scheme_aliases": [
            "PM-KMY",
            "PMKMY",
            "pm kmy",
            "Kisan Maandhan Yojana",
            "Kisan Mandhan Yojana",
            "kisan mandhan",
            "PMKMY Yojana",
        ],
    },
    "pm-rkvy": {
        "scheme_name": "Pradhan Mantri Rashtriya Krishi Vikas Yojana",
        "scheme_aliases": [
            "PM-RKVY",
            "PM RKVY",
            "pm rkvy",
            "RKVY",
            "rashtriya krishi vikas",
            "rashtriya krishi vikas yojana",
        ],
    },
    "pulses-mission": {
        "scheme_name": "Mission for Aatmanirbharta in Pulses",
        "scheme_aliases": [
            "pulses mission",
            "pulses scheme",
            "nfsm pulses",
            "aatmanirbharta in pulses",
            "mission for aatmanirbharta in pulses",
            "aatmanirbharta pulses mission",
            "National Food Security Mission – Pulses",
        ],
    },
    "rwbcis": {
        "scheme_name": "Restructured Weather Based Crop Insurance Scheme",
        "scheme_aliases": [
            "RWBCIS",
            "weather based crop insurance",
            "weather insurance",
            "weather based crop insurance scheme",
        ],
    },
}

# Legacy integrated schemes (get_scheme_info / Hasura). Not zero-redeploy.
BOOTSTRAP_LEGACY_SCHEMES: list[dict[str, str]] = [
    {"scheme_code": "kcc", "scheme_name": "Kisan Credit Card scheme", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "pmkisan", "scheme_name": "Pradhan Mantri Kisan Samman Nidhi scheme", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "pmfby", "scheme_name": "Pradhan Mantri Fasal Bima Yojana scheme", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "shc", "scheme_name": "Soil Health Card scheme", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "pmksy", "scheme_name": "Pradhan Mantri Krishi Sinchayee Yojana", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "sathi", "scheme_name": "SATHI Seed Authentication, Traceability & Holistic Inventory", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "pmasha", "scheme_name": "Pradhan Mantri Annadata Aay Sanrakshan Abhiyan", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "aif", "scheme_name": "Agriculture Infrastructure Fund", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "smam", "scheme_name": "Sub-Mission on Agricultural Mechanization", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "pdmc", "scheme_name": "Per Drop More Crop scheme", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "pkvy", "scheme_name": "Paramparagat Krishi Vikas Yojana", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "nfsm", "scheme_name": "National Food Security Mission", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "rad", "scheme_name": "Rainfed Area Development", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "ffs", "scheme_name": "Framework for Fertilizer Sales", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "nbm", "scheme_name": "National Bamboo Mission", "tool": "get_scheme_info", "tool_routing": "legacy"},
    {"scheme_code": "nbhm", "scheme_name": "National Beekeeping & Honey Mission", "tool": "get_scheme_info", "tool_routing": "legacy"},
]

ROUTING_EXCEPTIONS: dict[str, str] = {
    "pkvy": "search_schemes",
    "nbm": "get_scheme_info",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def central_instances() -> set[str]:
    raw = (os.environ.get("CENTRAL_INSTANCES") or "default").strip()
    return {p.strip().lower() for p in raw.split(",") if p.strip()} or {"default"}


def default_network_visible(instance: str | None) -> bool:
    inst = (instance or "default").strip().lower() or "default"
    return inst in central_instances()


def scheme_collection_name() -> str:
    return (
        os.environ.get("PROD_SCHEME_QDRANT_COLLECTION_NAME")
        or "schemes-index"
    ).strip()


def documents_collection_name() -> str:
    return (
        os.environ.get("PROD_QDRANT_COLLECTION_NAME")
        or os.environ.get("QDRANT_COLLECTION_NAME")
        or "documents-index"
    ).strip()


def validate_scheme_code(code: str) -> str:
    normalized = (code or "").strip().lower()
    if not normalized or not SCHEME_CODE_RE.match(normalized):
        raise ValueError(
            f"Invalid scheme_code '{code}'. Use lowercase slug: ^[a-z0-9]+(?:-[a-z0-9]+)*$"
        )
    return normalized


def parse_aliases(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(a).strip() for a in value if str(a).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(a).strip() for a in parsed if str(a).strip()]
        except json.JSONDecodeError:
            return [p.strip() for p in text.split(",") if p.strip()]
    return []


def _content_hash(code: str, name: str, aliases: list[str], network_visible: bool, status: str) -> str:
    payload = json.dumps(
        {
            "code": code,
            "name": name,
            "aliases": sorted(aliases),
            "network_visible": network_visible,
            "status": status,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def ensure_catalog_schema() -> None:
    """Create catalog tables and scheme metadata columns (idempotent)."""
    db.init_scheme_catalog_schema()


def bootstrap_catalog_if_empty(*, force: bool = False) -> dict[str, Any]:
    """Seed bootstrap vector + legacy schemes when catalog is empty (or force)."""
    ensure_catalog_schema()
    meta = db.get_catalog_meta()
    entries = db.list_catalog_entries()
    if entries and not force:
        return {"bootstrapped": False, "version": meta.get("version", 0), "entry_count": len(entries)}

    now = utc_now_iso()
    collection = scheme_collection_name()
    for code, definition in BOOTSTRAP_VECTOR_SCHEMES.items():
        aliases = list(definition.get("scheme_aliases") or [])
        name = definition["scheme_name"]
        status = "live"
        db.upsert_catalog_entry(
            scheme_code=code,
            scheme_name=name,
            scheme_aliases=aliases,
            instances=["default"],
            workflow_ids=[],
            collection_name=collection,
            chunk_count=0,
            status=status,
            network_visible=True,
            promoted_at=None,
            content_hash=_content_hash(code, name, aliases, True, status),
            source="bootstrap",
            updated_at=now,
        )
    version = db.bump_catalog_version("bootstrap_vector_schemes")
    return {
        "bootstrapped": True,
        "version": version,
        "vector_count": len(BOOTSTRAP_VECTOR_SCHEMES),
        "legacy_count": len(BOOTSTRAP_LEGACY_SCHEMES),
    }


def rebuild_catalog_from_documents(*, reason: str = "rebuild") -> int:
    """
    Recompute scheme_catalog_entries from documents table, then bump version.

    Pipeline-sourced live docs override bootstrap for the same scheme_code.
    Bootstrap-only codes with no live docs remain live (migration phase A1).
    """
    ensure_catalog_schema()
    now = utc_now_iso()
    collection = scheme_collection_name()

    docs = db.list_scheme_documents_for_catalog()
    by_code: dict[str, list[dict]] = {}
    for doc in docs:
        code = (doc.get("scheme_code") or "").strip().lower()
        if not code:
            continue
        by_code.setdefault(code, []).append(doc)

    # Track which codes are fully owned by pipeline live docs
    live_pipeline_codes: set[str] = set()

    for code, group in by_code.items():
        live_docs = [
            d
            for d in group
            if (d.get("stage") == "completed")
            and not int(d.get("is_disabled") or 0)
            and int(d.get("catalog_visible") if d.get("catalog_visible") is not None else 1)
            and (d.get("document_kind") or "document") == "scheme"
        ]
        pending_reindex = any(
            (d.get("stage") == "completed")
            and not int(d.get("is_disabled") or 0)
            and (d.get("reindex_required") or d.get("_pending_reindex"))
            for d in group
        )
        # Also treat reindex_reason containing scheme_code_rename as pending_reindex
        for d in group:
            reason_txt = (d.get("reindex_reason") or "")
            if "scheme_code_rename" in reason_txt and d.get("stage") == "completed" and not int(d.get("is_disabled") or 0):
                pending_reindex = True

        if pending_reindex and live_docs:
            # If any live doc is waiting reindex after rename, publish as pending_reindex
            # unless we only have non-rename live docs — keep simple: any reindex_required on scheme
            if any(int(d.get("reindex_required") or 0) for d in live_docs):
                status = "pending_reindex"
            else:
                status = "live"
        elif live_docs:
            status = "live"
            live_pipeline_codes.add(code)
        elif any(d.get("stage") == "approval_for_prod" and not int(d.get("is_disabled") or 0) for d in group):
            status = "pending_prod"
        else:
            status = "disabled"

        # Prefer live docs for aggregation; else all group
        use = live_docs or group
        names = [d.get("scheme_name") for d in use if d.get("scheme_name")]
        scheme_name = names[0] if names else code
        aliases: list[str] = []
        seen_alias: set[str] = set()
        for d in use:
            for a in parse_aliases(d.get("scheme_aliases_json")):
                key = a.lower()
                if key not in seen_alias:
                    seen_alias.add(key)
                    aliases.append(a)
        instances = sorted(
            {
                (d.get("instance") or "default").strip().lower() or "default"
                for d in use
            }
        )
        workflow_ids = [d["workflow_id"] for d in use if d.get("workflow_id")]
        chunk_count = sum(int(d.get("chunk_count") or 0) for d in use)
        network_visible = any(int(d.get("network_visible") or 0) for d in use)  # OR
        promoted_times = [d.get("ingested_at") or d.get("updated_at") for d in live_docs if d.get("ingested_at") or d.get("updated_at")]
        promoted_at = max(promoted_times) if promoted_times else None
        source = "pipeline" if any((d.get("document_kind") or "") == "scheme" for d in group) else "import"

        # pending_reindex entries must not appear as live for consumers
        if status == "pending_reindex":
            # still store entry but status excludes from vector_schemes
            pass

        db.upsert_catalog_entry(
            scheme_code=code,
            scheme_name=scheme_name,
            scheme_aliases=aliases,
            instances=instances,
            workflow_ids=workflow_ids,
            collection_name=collection,
            chunk_count=chunk_count,
            status=status if status != "disabled" or live_docs else "disabled",
            network_visible=1 if network_visible else 0,
            promoted_at=promoted_at,
            content_hash=_content_hash(code, scheme_name, aliases, network_visible, status),
            source=source,
            updated_at=now,
        )

    # Preserve bootstrap live entries for codes with no document rows;
    # disable orphan pipeline / import codes that have no docs left.
    existing = {e["scheme_code"]: e for e in db.list_catalog_entries()}
    for code, definition in BOOTSTRAP_VECTOR_SCHEMES.items():
        if code in by_code:
            continue
        if code in existing and existing[code].get("source") == "pipeline":
            # Was pipeline-owned but no longer eligible → mark disabled
            if code not in live_pipeline_codes:
                db.upsert_catalog_entry(
                    scheme_code=code,
                    scheme_name=existing[code].get("scheme_name") or definition["scheme_name"],
                    scheme_aliases=parse_aliases(existing[code].get("scheme_aliases_json")),
                    instances=parse_aliases(existing[code].get("instances_json")) or ["default"],
                    workflow_ids=parse_aliases(existing[code].get("workflow_ids_json")),
                    collection_name=collection,
                    chunk_count=int(existing[code].get("chunk_count") or 0),
                    status="disabled",
                    network_visible=int(existing[code].get("network_visible") or 0),
                    promoted_at=existing[code].get("promoted_at"),
                    content_hash=_content_hash(
                        code,
                        existing[code].get("scheme_name") or definition["scheme_name"],
                        parse_aliases(existing[code].get("scheme_aliases_json")),
                        bool(existing[code].get("network_visible")),
                        "disabled",
                    ),
                    source="pipeline",
                    updated_at=now,
                )
            continue
        # Keep or re-seed bootstrap
        aliases = list(definition.get("scheme_aliases") or [])
        name = definition["scheme_name"]
        db.upsert_catalog_entry(
            scheme_code=code,
            scheme_name=name,
            scheme_aliases=aliases,
            instances=["default"],
            workflow_ids=[],
            collection_name=collection,
            chunk_count=0,
            status="live",
            network_visible=1,
            promoted_at=None,
            content_hash=_content_hash(code, name, aliases, True, "live"),
            source="bootstrap",
            updated_at=now,
        )

    # Disable non-bootstrap entries that no longer appear in any document group
    for code, entry in existing.items():
        if code in by_code or code in BOOTSTRAP_VECTOR_SCHEMES:
            continue
        if (entry.get("status") or "") == "disabled":
            continue
        name = entry.get("scheme_name") or code
        aliases = parse_aliases(entry.get("scheme_aliases_json"))
        db.upsert_catalog_entry(
            scheme_code=code,
            scheme_name=name,
            scheme_aliases=aliases,
            instances=parse_aliases(entry.get("instances_json")) or ["default"],
            workflow_ids=[],
            collection_name=collection,
            chunk_count=int(entry.get("chunk_count") or 0),
            status="disabled",
            network_visible=int(entry.get("network_visible") or 0),
            promoted_at=entry.get("promoted_at"),
            content_hash=_content_hash(code, name, aliases, bool(entry.get("network_visible")), "disabled"),
            source=entry.get("source") or "pipeline",
            updated_at=now,
        )

    return db.bump_catalog_version(reason)


def on_scheme_document_promoted(workflow_id: str) -> Optional[int]:
    """Call after successful PROD promote of a scheme document."""
    doc = db.get_document(workflow_id)
    if not doc:
        return None
    if (doc.get("document_kind") or "document") != "scheme":
        return None
    # Clear reindex flag on successful promote (new code is live)
    if int(doc.get("reindex_required") or 0):
        db.update_document_fields(
            workflow_id,
            reindex_required=0,
            reindex_reason=None,
        )
    return rebuild_catalog_from_documents(reason=f"promote_scheme:{workflow_id}")


def on_scheme_document_disabled(workflow_id: str, scheme_code: Optional[str] = None) -> Optional[int]:
    return rebuild_catalog_from_documents(reason=f"disable_scheme:{workflow_id}")


def build_tool_prompts(vector_schemes: list[dict[str, Any]]) -> dict[str, str]:
    """Fragments for AI system prompts / tool docstrings."""
    codes = [s["scheme_code"] for s in vector_schemes]
    bullets = []
    flat = []
    identifiers = []
    for s in vector_schemes:
        code = s["scheme_code"]
        name = s.get("scheme_name") or code
        bullets.append(f"- **{name}** ({code})")
        flat.append(f"- {name} ({code})")
        aliases = s.get("scheme_aliases") or []
        alias_hint = f" / {aliases[0]}" if aliases else ""
        identifiers.append(f"- `{code}`{alias_hint}")
    legacy_codes = ", ".join(f'"{s["scheme_code"]}"' for s in BOOTSTRAP_LEGACY_SCHEMES)
    search_doc = (
        "Available Qdrant scheme codes: "
        + ", ".join(f'"{c}"' for c in codes)
        + ". Use search_schemes for guideline PDF chunks (Government Scheme Information)."
    )
    return {
        "search_schemes_doc": search_doc,
        "vector_schemes_bullets_en": "\n".join(bullets),
        "legacy_schemes_codes_line": legacy_codes,
        "available_schemes_flat_en": "\n".join(flat),
        "vector_identifiers_block_en": "\n".join(identifiers),
        "vector_scheme_count": str(len(vector_schemes)),
        "legacy_scheme_count": str(len(BOOTSTRAP_LEGACY_SCHEMES)),
    }


def build_snapshot(
    *,
    instance: Optional[str] = None,
    status: str = "live",
    include_pending: bool = False,
    network_visible: Optional[bool] = None,
) -> dict[str, Any]:
    ensure_catalog_schema()
    meta = db.get_catalog_meta()
    version = int(meta.get("version") or 0)
    updated_at = meta.get("updated_at") or utc_now_iso()

    statuses = {status} if status else {"live"}
    if include_pending:
        statuses |= {"pending_prod", "pending_reindex"}

    entries = db.list_catalog_entries()
    vector_schemes: list[dict[str, Any]] = []
    for e in entries:
        st = e.get("status") or "disabled"
        if st not in statuses:
            continue
        # Live snapshot for warmers: only status=live (pending excluded unless include_pending)
        if not include_pending and st != "live":
            continue
        instances = parse_aliases(e.get("instances_json"))
        if instance:
            inst = instance.strip().lower()
            if inst not in {i.lower() for i in instances} and instances:
                # bootstrap may have ["default"] only
                if inst not in {i.lower() for i in instances}:
                    continue
        nv = bool(int(e.get("network_visible") or 0))
        if network_visible is not None and nv != network_visible:
            continue
        vector_schemes.append(
            {
                "scheme_code": e["scheme_code"],
                "scheme_name": e.get("scheme_name") or e["scheme_code"],
                "scheme_aliases": parse_aliases(e.get("scheme_aliases_json")),
                "tool": "search_schemes",
                "tool_routing": "qdrant",
                "instances": instances or ["default"],
                "network_visible": nv,
                "chunk_count": int(e.get("chunk_count") or 0),
                "workflow_ids": parse_aliases(e.get("workflow_ids_json")),
                "promoted_at": e.get("promoted_at"),
                "source": e.get("source") or "bootstrap",
                "status": st,
            }
        )

    vector_schemes.sort(key=lambda s: s["scheme_code"])
    etag_src = f"{version}-" + hashlib.sha256(
        json.dumps([s["scheme_code"] for s in vector_schemes]).encode()
    ).hexdigest()[:8]
    etag = f'"{etag_src}"'

    return {
        "api_version": "1",
        "catalog_version": version,
        "updated_at": updated_at,
        "etag": etag,
        "collections": {
            "scheme_qdrant": {
                "name": scheme_collection_name(),
                "embedding_model": "intfloat/multilingual-e5-large",
                "filter_type": "scheme",
            },
            "documents": {
                "name": documents_collection_name(),
            },
        },
        "legacy_schemes": list(BOOTSTRAP_LEGACY_SCHEMES),
        "vector_schemes": vector_schemes,
        "tool_prompts": build_tool_prompts(
            [s for s in vector_schemes if s.get("status", "live") == "live"]
            if include_pending
            else vector_schemes
        ),
        "routing_exceptions": dict(ROUTING_EXCEPTIONS),
    }


def apply_scheme_metadata(
    workflow_id: str,
    *,
    document_kind: Optional[str] = None,
    scheme_code: Optional[str] = None,
    scheme_name: Optional[str] = None,
    scheme_aliases: Optional[list[str]] = None,
    tool_routing: Optional[str] = None,
    catalog_visible: Optional[bool] = None,
    network_visible: Optional[bool] = None,
) -> dict[str, Any]:
    """Update document scheme fields and rebuild catalog when needed."""
    ensure_catalog_schema()
    doc = db.get_document(workflow_id)
    if not doc:
        raise LookupError(f"Document not found: {workflow_id}")

    updates: dict[str, Any] = {}
    old_code = (doc.get("scheme_code") or "").strip().lower() or None
    old_kind = (doc.get("document_kind") or "document").strip().lower()
    code_renamed = False
    pending_reindex = False

    if document_kind is not None:
        kind = document_kind.strip().lower()
        if kind not in ("document", "scheme"):
            raise ValueError("document_kind must be 'document' or 'scheme'")
        updates["document_kind"] = kind
    else:
        kind = old_kind

    new_code = old_code
    if scheme_code is not None:
        new_code = validate_scheme_code(scheme_code) if scheme_code else None
        updates["scheme_code"] = new_code
        if old_code and new_code and old_code != new_code:
            code_renamed = True

    if scheme_name is not None:
        updates["scheme_name"] = scheme_name.strip() if scheme_name else None

    if scheme_aliases is not None:
        updates["scheme_aliases_json"] = json.dumps(parse_aliases(scheme_aliases))

    if tool_routing is not None:
        tr = tool_routing.strip().lower()
        if tr not in ("qdrant", "legacy", "both"):
            raise ValueError("tool_routing must be qdrant|legacy|both")
        updates["tool_routing"] = tr
    elif kind == "scheme" and not doc.get("tool_routing"):
        updates["tool_routing"] = "qdrant"

    if catalog_visible is not None:
        updates["catalog_visible"] = 1 if catalog_visible else 0
    elif kind == "scheme" and doc.get("catalog_visible") is None:
        updates["catalog_visible"] = 1

    if network_visible is not None:
        updates["network_visible"] = 1 if network_visible else 0
    elif kind == "scheme" and doc.get("network_visible") is None:
        updates["network_visible"] = 1 if default_network_visible(doc.get("instance")) else 0

    if kind == "scheme" and not (new_code or updates.get("scheme_code") or old_code):
        # Allow setting kind first without code only if not completing as scheme
        pass

    if code_renamed and (doc.get("stage") == "completed") and not int(doc.get("is_disabled") or 0):
        # Rule (b): hide new code until reindex
        updates["reindex_required"] = 1
        updates["reindex_reason"] = f"scheme_code_rename:{old_code}->{new_code}"
        pending_reindex = True

    if updates:
        db.update_document_fields(workflow_id, **updates)

    # Rebuild catalog if scheme-related
    final = db.get_document(workflow_id) or doc
    final_kind = (final.get("document_kind") or "document").strip().lower()
    catalog_version = None
    if final_kind == "scheme" or old_kind == "scheme" or code_renamed:
        reason = "scheme_metadata_patch"
        if pending_reindex:
            reason = "scheme_code_rename_pending_reindex"
        catalog_version = rebuild_catalog_from_documents(reason=reason)

    return {
        "document": final,
        "catalog_version": catalog_version,
        "code_renamed": code_renamed,
        "pending_reindex": pending_reindex,
        "requires_reindex": pending_reindex,
    }
