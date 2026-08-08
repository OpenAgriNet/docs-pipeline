"""
FastAPI REST API for the Temporal-based OCR pipeline.

This API provides HTTP endpoints that interact with Temporal workflows.
"""

import os
import json
import asyncio
import hashlib
import logging
import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request
from temporalio.client import Client
from minio import Minio

from urllib.parse import quote

from .app import app  # noqa: F401  (re-exported: `from pipeline.api import app`)
from .models import (
    DocumentDetail, DocumentSummary, DocumentStage, PIPELINE_STAGES,
    BulkWorkflowActionRequest, BulkWorkflowActionResponse, BulkWorkflowActionResult,
)
from . import clients
from . import db
from . import keycloak_admin
from .keycloak_admin import KeycloakAdminError, KeycloakAdminUnconfigured
from .vector_store import (
    VectorStore,
    any_of_filter,
    field_filter,
    get_vector_store,
    is_valid_logical_index_name,
    physical_index_name,
)
# Names that used to be DEFINED here and are now re-exported from the seam, so
# `pipeline.api`'s module surface is unchanged by the extraction. The two probes
# in particular stay reachable as `api._index_has_*_field`: they are handle-first
# (they take a live index, not a name) and callers/tests address them that way.
from .vector_store import (  # noqa: F401  (re-exported for compatibility)
    MarqoPurgeScopeError,
    MarqoPurgeUnconfirmedError,
    get_legacy_marqo_doc_id,
    index_has_instance_field as _index_has_instance_field,
    index_has_workflow_id_field as _index_has_workflow_id_field,
    index_missing_error as _marqo_index_missing,
    marqo_doc_scope_filter as _marqo_doc_scope_filter,
    purge_document as _marqo_purge_document,
    purge_ids as _marqo_purge_ids,
    default_physical_index as _default_physical_index,
    _MARQO_PURGE_PAGE,
)
from .auth.deps import assert_permission_in_instance
from .auth.models import AuthUser
from .auth.permissions import Permission
from .auth.tenancy import (
    allowed_instances,
    assert_document_instance_access,
    assert_instance_access,
    default_instance,
    normalize_instance,
    user_can_access_instance,
)

TASK_QUEUE = clients.TASK_QUEUE
_TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)

# Cached clients. These stay module-level attributes on purpose: they are the
# cache the accessors below read and fill, so `monkeypatch.setattr(api,
# "temporal_client", ...)` / `"minio_client"` keeps working exactly as before.
# Nothing connects at import or at startup any more — see get_temporal_client().
temporal_client: Optional[Client] = None
minio_client: Optional[Minio] = None
MINIO_BUCKET = clients.MINIO_BUCKET


async def get_temporal_client() -> Client:
    """Return the Temporal client, connecting on first use.

    Reads/fills the module-level ``temporal_client`` so an injected (test) client
    is always honoured. Connection failures propagate to the caller.
    """
    global temporal_client
    if temporal_client is None:
        temporal_client = await clients.get_temporal_client()
    return temporal_client


async def _temporal_client_or_none() -> Optional[Client]:
    """Temporal client for *reporting* callers: None instead of raising.

    Only for endpoints whose contract is "tell me the state of the world"
    (/health, the runtime probe). Every route that actually needs Temporal to do
    work must call get_temporal_client() and let the failure surface.
    """
    try:
        return await asyncio.wait_for(get_temporal_client(), timeout=5.0)
    except Exception as exc:  # noqa: BLE001 - health must not 500 on an outage
        logging.warning("Temporal unavailable: %s", exc)
        return None


def get_minio_client() -> Minio:
    """Return the MinIO client, constructing it (and its bucket) on first use."""
    global minio_client
    if minio_client is None:
        minio_client = clients.get_minio_client()
    return minio_client


# Allowed base directories for file access (configurable via env)
ALLOWED_FILE_PATHS = os.environ.get("ALLOWED_FILE_PATHS", "/app/books,/data/documents").split(",")
ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"
}


def validate_file_path(filepath: str) -> str:
    """
    Validate that a file path is within allowed directories.
    Prevents path traversal attacks.

    Returns the validated filepath as a string.
    Raises HTTPException if path is not allowed.
    """
    # Handle minio:// URIs - these are validated by MinIO access
    if filepath.startswith("minio://"):
        suffix = Path(filepath).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(400, f"Unsupported file type: {suffix}")
        return filepath  # Return string as-is, MinIO handles validation

    path = Path(filepath).resolve()  # Resolve to absolute, canonical path

    # Check if path is within any allowed directory
    for allowed_base in ALLOWED_FILE_PATHS:
        allowed_path = Path(allowed_base.strip()).resolve()
        try:
            path.relative_to(allowed_path)
            # Path is within allowed directory
            if not path.exists():
                raise HTTPException(404, "File not found")
            if not path.is_file():
                raise HTTPException(400, "Path is not a file")
            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                raise HTTPException(400, f"Unsupported file type: {path.suffix.lower()}")
            return str(path)
        except ValueError:
            continue  # Not within this allowed path, try next

    # Path not within any allowed directory
    raise HTTPException(403, "Access to this file path is not allowed")


def get_filename_from_path(filepath: str) -> str:
    """Extract filename from a filepath string (works for both local and minio:// paths)."""
    if filepath.startswith("minio://"):
        # minio://bucket/path/to/file.pdf -> file.pdf
        return filepath.split("/")[-1]
    return Path(filepath).name


def get_workflow_id(filepath: str) -> str:
    """Generate consistent workflow ID from filepath."""
    return f"doc-{hashlib.md5(filepath.encode()).hexdigest()[:12]}"


def _rerun_workflow_id(base_workflow_id: str) -> str:
    """Generate a fresh workflow ID for explicit reruns of the same source."""
    return f"{base_workflow_id}-rerun-{int(time.time())}"


def _tenant_workflow_id(base_workflow_id: str, instance: str) -> str:
    """Make a workflow id self-describing + collision-safe across tenants.

    Inert by default: for the default (single-tenant) instance the existing id
    scheme is returned unchanged, so dedup / reuse behaviour is identical to
    today. A real, non-default tenant yields ``wf-<instance>-<base_id>`` so the
    same source file in two tenants never collides.
    """
    inst = normalize_instance(instance)
    if inst == default_instance():
        return base_workflow_id
    return f"wf-{inst}-{base_workflow_id}"


# Feature-detect once whether the Temporal namespace has the `Instance` search
# attribute registered. None = unknown, True/False = detected. Avoids repeatedly
# attempting (and failing) a start with an unregistered attribute.
_instance_search_attr_supported: Optional[bool] = None


async def _start_pipeline_workflow(run, *, args: list, id: str, instance: str):
    """Start a workflow tagged with its owning tenant.

    - Always attaches a Temporal **memo** ``{"instance": <instance>}`` (memos need
      no server-side registration and are inert metadata).
    - Best-effort attaches an ``Instance`` **search attribute** so executions can
      be filtered by tenant in the Temporal UI/API. This requires registering the
      ``Instance`` keyword search attribute in the namespace; if it isn't
      registered the start would fail, so we feature-detect once and fall back to
      memo-only, caching the result. Genuine start failures (duplicate id, etc.)
      are never swallowed.
    """
    global _instance_search_attr_supported
    inst = normalize_instance(instance)
    memo = {"instance": inst}

    if _instance_search_attr_supported is not False:
        try:
            handle = await (await get_temporal_client()).start_workflow(
                run,
                args=args,
                id=id,
                task_queue=TASK_QUEUE,
                memo=memo,
                search_attributes={"Instance": [inst]},
            )
            _instance_search_attr_supported = True
            return handle
        except Exception as exc:  # noqa: BLE001 - narrow to search-attr issues below
            msg = str(exc).lower()
            if "search attribute" not in msg and "searchattribute" not in msg:
                raise
            _instance_search_attr_supported = False
            logging.info(
                "Temporal `Instance` search attribute is not registered; "
                "starting with memo only. Register it to enable UI filtering."
            )

    return await (await get_temporal_client()).start_workflow(
        run,
        args=args,
        id=id,
        task_queue=TASK_QUEUE,
        memo=memo,
    )


def _compute_file_fingerprint(filepath: Path) -> str:
    md5 = hashlib.md5()
    with filepath.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()


def _ignore_client_marqo_url(_client_supplied: str = "") -> str:
    """Always resolve Marqo from the environment at ingest time; ignore client URLs (SSRF)."""
    return ""


def _instance_scope_for_user(user: AuthUser) -> Optional[list[str]]:
    """None = unrestricted; otherwise only these instance ids."""
    allowed = allowed_instances(user)
    if allowed is None:
        return None
    return sorted(allowed)


# Log the "legacy index, skipping tenant filter" note at most once per process.
_MARQO_INSTANCE_FILTER_SKIP_LOGGED = False


def _marqo_instance_filter(user: AuthUser, index) -> Optional[str]:
    """Marqo filter clause scoping search results to the caller's instances.

    Returns ``None`` (no filter) ONLY for a data-unrestricted caller (local
    bypass). For a **restricted** caller we always AND-in a scoping clause:

    * index advertises the ``instance`` field → ``instance:(<allowed...>)``.
    * index has NO ``instance`` field (legacy single-tenant index) → we FAIL
      CLOSED with ``instance:(__none__)`` (match nothing) rather than returning
      ``None``. Returning ``None`` here would give a restricted caller an
      UNFILTERED read over that index's entire corpus — the tolerant/no-filter
      shortcut is reserved for unrestricted callers only.

    Never raises.
    """
    allowed = allowed_instances(user)
    if allowed is None:
        return None
    if not _index_has_instance_field(index):
        global _MARQO_INSTANCE_FILTER_SKIP_LOGGED
        # Legacy single-tenant index (no filterable `instance` field) == the
        # DEFAULT tenant's corpus. A caller entitled to the default instance owns
        # ALL of it, so no scoping clause applies. Any other caller must match
        # nothing — but we must NOT reference the absent `instance` field, or
        # Marqo 400s ("no filterable field 'instance'") and every read/purge on a
        # legacy index fails. Use a `doc_id`
        # sentinel instead — a field every index has.
        if default_instance() in {str(i).strip().lower() for i in allowed}:
            return None
        if not _MARQO_INSTANCE_FILTER_SKIP_LOGGED:
            logging.debug(
                "Marqo index has no `instance` field; a non-default caller is "
                "failed closed (match nothing) on this legacy single-tenant index."
            )
            _MARQO_INSTANCE_FILTER_SKIP_LOGGED = True
        return field_filter("doc_id", "__none__")
    if not allowed:
        # Restricted user with an empty instance set: match nothing.
        return field_filter("instance", "__none__")
    return any_of_filter("instance", sorted(allowed))


def _resolve_create_instance(user: AuthUser, requested: Optional[str] = None) -> str:
    """Normalize the create-time instance and ensure the caller may UPLOAD into it.

    ``assert_instance_access`` only proves the caller can *reach* the target
    tenant; it does not prove the caller may *write* there. A caller who is (say)
    a viewer in tenant-A but a curator in tenant-B passes the any-instance
    ``RequireUpload`` gate via tenant-B, yet must NOT be able to create documents
    in tenant-A. Assert the ``upload`` permission in the resolved tenant (403 on a
    reachable-but-wrong-role tenant).
    """
    inst = assert_instance_access(user, requested or default_instance())
    assert_permission_in_instance(user, inst, Permission.UPLOAD)
    return inst


# =============================================================================
# Search index registry (Phase 4) — logical (instance, index) -> physical Marqo
# =============================================================================


def _new_marqo_index_name(instance: str, name: str) -> str:
    """Physical name for a newly-provisioned index: ``<ns><instance>-<name>``.

    The name POLICY (charset, namespace, join formula) lives in
    ``pipeline.vector_store``; what a REJECTED name means is this surface's own
    decision and stays here. An API caller typed the name, so a bad one is a 400
    — where the ingest-side registry, which is handed a name rather than told
    one, falls back to the tenant's own default instead. The two reactions have
    always differed; only the naming rule was duplicated.
    """
    clean = (name or "").strip().lower()
    if not is_valid_logical_index_name(clean):
        raise HTTPException(400, "index name must match ^[a-z0-9_]{1,40}$ (letters, digits, _ only)")
    return physical_index_name(normalize_instance(instance), clean)


def resolve_index(instance: str | None, name: Optional[str] = None) -> Optional[str]:
    """Resolve ``(instance, optional logical name)`` to a physical Marqo index.

    Registry-backed; replaces bare default-index env reads in the search /
    read paths. ``name=None`` yields the tenant's default index.

    Resolution outcomes:

    * registry hit -> that physical index.
    * a *named* index that isn't registered -> 404 (it does not exist).
    * ``name=None`` with no registered default:
        - the **DEFAULT** instance falls back to the legacy physical index
          (single-tenant back-compat — the seeded default -> legacy mapping).
        - **any other tenant** returns ``None``: that tenant simply has no index.
          It NEVER falls back to another tenant's (the default's) physical index —
          doing so would return one tenant's documents to another on a READ.

    Callers MUST handle ``None`` gracefully (empty result / "no index"), never
    substituting a fallback index.
    """
    inst = normalize_instance(instance)
    physical = db.resolve_marqo_index(inst, name)
    if physical:
        return physical
    if name:
        raise HTTPException(404, "Index not found")
    if inst == default_instance():
        return _default_physical_index()
    return None


def assert_index_access(user: AuthUser, instance: str | None, name: Optional[str] = None) -> Optional[str]:
    """Validate index -> owning-tenant -> caller-access, returning the physical index.

    Confirms the caller may address the logical index ``name`` within ``instance``:
    the tenant must be in the caller's ``allowed_instances`` (unrestricted admins
    pass). Cross-tenant / non-existent indexes return **404** (never 403) so index
    existence is not leaked.

    When ``name=None`` and the tenant has no registered default index, mirrors
    :func:`resolve_index`: the **DEFAULT** instance falls back to the legacy
    physical index (single-tenant back-compat), but any **other** tenant returns
    ``None`` (it has no index) — NEVER another tenant's physical index. Callers
    must handle ``None`` gracefully rather than substituting a fallback.
    """
    inst = normalize_instance(instance)
    if not user_can_access_instance(user, inst):
        raise HTTPException(404, "Index not found")
    physical = db.resolve_marqo_index(inst, name)
    if physical is None:
        if name:
            raise HTTPException(404, "Index not found")
        if inst == default_instance():
            physical = _default_physical_index()
        else:
            return None
    return physical


def assert_marqo_index_access(user: AuthUser, marqo_index: str) -> str:
    """Validate access to a *physical* Marqo index supplied directly by a caller.

    Reverse-resolves the physical index to its owning tenant via the registry and
    confirms caller access (404 if not). An unregistered physical index is the
    transitional legacy single-index case: only unrestricted callers may address
    it directly; the per-chunk ``instance`` filter then scopes results.
    """
    inst = (marqo_index or "").strip()
    row = db.get_index_by_marqo_index(inst)
    if row is not None:
        if not user_can_access_instance(user, row["instance"]):
            raise HTTPException(404, "Index not found")
        return inst
    # Unregistered physical index (legacy). Restricted callers cannot target it
    # by physical name; they must go through (instance, index) resolution.
    if allowed_instances(user) is not None:
        raise HTTPException(404, "Index not found")
    return inst


def _assert_can_manage_indexes(user: AuthUser, instance: str | None) -> str:
    """Gate index create/delete: caller needs ``admin`` or ``pipeline`` *in* the tenant.

    Managing a tenant's indexes is a DATA-plane / tenant operation, so a pure
    ``master_admin`` (control plane, not a member of this tenant) is rejected
    here. Because a platform admin legitimately knows the tenant exists (it owns
    the tenant registry), it gets a 403 rather than a 404 existence-hide.
    Cross-tenant access by a non-platform caller is hidden as 404; a reachable
    tenant with an insufficient role is 403.
    """
    inst = normalize_instance(instance)
    if not user_can_access_instance(user, inst):
        if user.is_platform_admin:
            raise HTTPException(403, "Managing a tenant's indexes requires admin or pipeline in that tenant")
        raise HTTPException(404, "Tenant not found")
    perms = user.permissions_in(inst)
    if Permission.ADMIN not in perms and Permission.PIPELINE not in perms:
        raise HTTPException(403, "Requires admin or pipeline in tenant")
    return inst


def _assert_can_view_tenant(user: AuthUser, instance: str | None) -> str:
    """Gate tenant/index listing: any DATA access to the tenant (else 404, no leak).

    A tenant's index list is data-plane, so a pure ``master_admin`` (control
    plane) with no membership in this tenant is rejected. A platform admin gets a
    403 (it knows the tenant exists); a non-platform caller gets 404 (no leak).
    """
    inst = normalize_instance(instance)
    if not user_can_access_instance(user, inst):
        if user.is_platform_admin:
            raise HTTPException(403, "Viewing a tenant's indexes requires membership in that tenant")
        raise HTTPException(404, "Tenant not found")
    return inst


class _IndexSettingsView:
    """Adapts ``(store, physical index name)`` to the ``.get_settings()`` shape.

    ``_marqo_instance_filter`` and the capability probes it uses are handle-first
    because that is how the purge path uses them. This view lets a route hand
    them an index without holding a raw backend client, keeping the store the
    only thing that knows how to build one.
    """

    __slots__ = ("_store", "_index_name")

    def __init__(self, store: VectorStore, index_name: str) -> None:
        self._store = store
        self._index_name = index_name

    def get_settings(self) -> dict:
        return self._store.get_settings(self._index_name)


def _create_marqo_index_with_schema(
    marqo_index: str,
    embedding_model: Optional[str] = None,
    settings_override: Optional[dict] = None,
) -> dict:
    """Create a physical Marqo index with the canonical passage schema (idempotent)."""
    from .vector_store import passage_index_settings

    store = get_vector_store()
    settings = passage_index_settings(model=embedding_model, overrides=settings_override)
    if store.index_exists(marqo_index):
        # Never silently ADOPT a pre-existing physical index. If it is already this
        # tenant's registered index the (idempotent) re-create is a no-op; but an
        # unregistered physical index of the same name is a foreign/orphan index we
        # must not hand to a new tenant — refuse with 409 rather than adopt it.
        if db.get_index_by_marqo_index(marqo_index) is None:
            raise HTTPException(
                409,
                f"Physical Marqo index '{marqo_index}' already exists and is not "
                "registered to this tenant; refusing to adopt it.",
            )
        return settings
    store.create_index(marqo_index, settings)
    return settings


def _require_document_for_user(
    workflow_id: str,
    user: AuthUser,
    permission: Optional[Permission] = None,
) -> dict:
    """Load a document or 404 if missing / outside the caller's instance scope.

    When ``permission`` is given (doc-scoped *mutating* routes), additionally
    assert the caller holds that permission **in the document's tenant** — a
    valid-tenant-but-wrong-role mutation raises 403 while cross-tenant access
    stays 404. The route's ``RequireX`` dependency remains the any-instance gate;
    this narrows it to the acting tenant.
    """
    doc = assert_document_instance_access(user, db.get_document(workflow_id))
    if permission is not None:
        assert_permission_in_instance(user, doc.get("instance"), permission)
    return doc


def _document_for_user_or_none(
    workflow_id: str,
    user: AuthUser,
    permission: Optional[Permission] = None,
) -> Optional[dict]:
    """Like _require_document_for_user but returns None instead of raising (bulk paths).

    When ``permission`` is given, a doc the caller can reach but lacks the role
    for in that tenant is treated as inaccessible (returns None).
    """
    doc = db.get_document(workflow_id)
    if not doc or not user_can_access_instance(user, doc.get("instance")):
        return None
    if permission is not None and permission not in user.permissions_in(doc.get("instance") or ""):
        return None
    return doc


def _document_summary_from_row(doc: dict, current_job: Optional[dict] = None) -> DocumentSummary:
    return DocumentSummary(
        document_id=doc["document_id"],
        canonical_document_id=doc.get("canonical_document_id"),
        workflow_id=doc["workflow_id"],
        filename=doc["filename"],
        display_name=doc.get("display_name"),
        source_filename=doc.get("source_filename"),
        source_manifest_name=doc.get("source_manifest_name"),
        source_file_fingerprint=doc.get("source_file_fingerprint"),
        authoritative=bool(doc.get("source_manifest_name")),
        instance=normalize_instance(doc.get("instance")),
        is_demo=bool(doc.get("is_demo")),
        is_disabled=bool(doc.get("is_disabled")),
        query_enabled=bool(doc["query_enabled"]) if doc.get("query_enabled") is not None else True,
        stage=DocumentStage(doc["stage"]),
        page_count=doc.get("page_count") or 0,
        chunk_count=doc.get("chunk_count") or 0,
        error_message=doc.get("error_message"),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
        reindex_required=bool(doc.get("reindex_required")),
        reindex_reason=doc.get("reindex_reason"),
        available_actions=_list_available_actions(
            doc,
            current_job if current_job is not None else db.get_latest_document_job(doc["workflow_id"]),
        ),
    )


def _provenance_base_urls(request: Request) -> tuple[str, str]:
    api_base = (os.environ.get("DOCS_PIPELINE_API_URL") or str(request.base_url)).rstrip("/")
    ui_base = (os.environ.get("DOCS_PIPELINE_UI_URL") or "http://localhost:3000").rstrip("/")
    return api_base, ui_base


def _build_provenance_links(workflow_id: str, chunk_num: int, request: Request) -> dict[str, str]:
    api_base, ui_base = _provenance_base_urls(request)
    return {
        "pdf_url": f"{api_base}/documents/{workflow_id}/pdf",
        "document_url": f"{ui_base}/documents/{workflow_id}",
        "chunk_url": f"{ui_base}/documents/{workflow_id}?tab=chunks&chunk={chunk_num}",
    }


def _list_available_actions(doc: dict, current_job: Optional[dict] = None) -> list[str]:
    if not doc:
        return []
    if doc.get("is_disabled"):
        return ["restore_document"]

    stage = doc.get("stage")
    actions = ["disable_document", "reconcile_document", "set_query_enabled", "set_metadata"]
    if stage == "ocr_review":
        actions.append("approve_ocr")
    elif stage == "translation_review":
        actions.append("approve_translation")
    elif stage == "chunk_review":
        actions.append("approve_chunks")
    elif stage == "ready_for_ingestion":
        actions.append("approve_ingestion")
    elif stage == "completed":
        actions.append("reingest_document")
    elif stage == "failed":
        if not doc.get("ocr_completed_at"):
            actions.append("retry_ocr")
        if doc.get("ocr_completed_at") and not doc.get("translation_completed_at"):
            actions.append("retry_translation")
        if doc.get("translation_completed_at"):
            actions.append("retry_chunking")

    if doc.get("reindex_required"):
        actions.extend(["reingest_document", "clear_reindex_required"])
    else:
        actions.append("mark_reindex_required")

    if current_job and current_job.get("status") == "running":
        actions.append("inspect_runtime")

    return sorted(set(actions))


def _mark_reindex_required(workflow_id: str, reason: str, metadata: Optional[dict] = None) -> Optional[dict]:
    doc = db.mark_document_reindex_required(workflow_id, True, reason)
    if doc:
        db.log_audit(
            workflow_id=workflow_id,
            document_id=doc.get("document_id", workflow_id),
            action_type="mark_reindex_required",
            field_name="reindex_required",
            old_value="false",
            new_value="true",
            metadata={"reason": reason, **(metadata or {})},
        )
    return doc


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _tokenize(value: str) -> list[str]:
    return _TOKEN_RE.findall(_normalize_text(value))


def _prepare_query_for_e5(query: str) -> str:
    cleaned = query.strip()
    if cleaned.lower().startswith("query:"):
        return cleaned
    return f"query: {cleaned}"


def _token_overlap_score(query: str, text: str) -> float:
    query_tokens = set(_tokenize(query))
    text_tokens = set(_tokenize(text))
    if not query_tokens or not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def _metadata_blob(hit: dict) -> str:
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


def _rank_desc(values: list[float]) -> list[int]:
    order = sorted(range(len(values)), key=lambda idx: values[idx], reverse=True)
    ranks = [0] * len(values)
    for pos, idx in enumerate(order, start=1):
        ranks[idx] = pos
    return ranks


def _expand_query(query: str, profile: str) -> str:
    q = (query or "").strip()
    mode = (profile or "none").strip().lower()
    if not q or mode in {"none", ""}:
        return q
    if mode not in {"gu-v1", "gu_v1"}:
        return q

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

    additions: list[str] = []
    query_lower = q.lower()
    for pattern, terms in rules:
        if re.search(pattern, query_lower, flags=re.IGNORECASE):
            additions.append(terms)
    if not additions:
        return q
    return f"{q} {' '.join(additions)}".strip()


def _bm25lite_scores(query: str, docs: list[str]) -> list[float]:
    query_tokens = _tokenize(query)
    if not query_tokens or not docs:
        return [0.0] * len(docs)

    doc_tokens = [_tokenize(doc) for doc in docs]
    avg_len = max(1.0, sum(len(tokens) for tokens in doc_tokens) / max(1, len(doc_tokens)))
    df: Counter[str] = Counter()
    for tokens in doc_tokens:
        for token in set(tokens):
            df[token] += 1

    k1 = 1.2
    b = 0.75
    scores: list[float] = []
    for tokens in doc_tokens:
        tf = Counter(tokens)
        dl = len(tokens)
        norm = k1 * (1 - b + b * dl / avg_len)
        score = 0.0
        for term in query_tokens:
            if term not in tf:
                continue
            idf = math.log(1.0 + (len(doc_tokens) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (tf[term] * (k1 + 1.0)) / (tf[term] + norm)
        scores.append(score)
    return scores


def _rerank_hits(query: str, hits: list[dict], rerank_mode: str) -> list[dict]:
    mode = (rerank_mode or "none").strip().lower()
    if mode in {"", "none"} or not hits:
        return hits

    raw_scores = [float(hit.get("_score", hit.get("score", 0.0)) or 0.0) for hit in hits]
    min_score = min(raw_scores)
    max_score = max(raw_scores)
    denom = (max_score - min_score) if max_score > min_score else 1.0
    semantic_scores = [(raw - min_score) / denom for raw in raw_scores]
    text_scores = [_token_overlap_score(query, str(hit.get("text") or "")) for hit in hits]
    meta_scores = [_token_overlap_score(query, _metadata_blob(hit)) for hit in hits]

    rescored: list[dict] = []
    if mode == "bm25lite":
        docs = [f"{str(hit.get('text') or '')} {_metadata_blob(hit)}".strip() for hit in hits]
        bm_scores = _bm25lite_scores(query, docs)
        bm_min = min(bm_scores) if bm_scores else 0.0
        bm_max = max(bm_scores) if bm_scores else 1.0
        bm_denom = (bm_max - bm_min) if bm_max > bm_min else 1.0
        bm_norm = [(score - bm_min) / bm_denom for score in bm_scores]
        for hit, semantic, bm25, meta in zip(hits, semantic_scores, bm_norm, meta_scores):
            enriched = dict(hit)
            enriched["_rerank_score"] = (0.50 * semantic) + (0.40 * bm25) + (0.10 * meta) + (-0.10 if bool(hit.get("is_reference", False)) else 0.0)
            rescored.append(enriched)
    elif mode in {"rrf-lite", "rrf_lite", "rrf"}:
        sem_rank = _rank_desc(semantic_scores)
        text_rank = _rank_desc(text_scores)
        meta_rank = _rank_desc(meta_scores)
        k = 30
        for idx, hit in enumerate(hits):
            enriched = dict(hit)
            enriched["_rerank_score"] = (1.0 / (k + sem_rank[idx])) + (1.0 / (k + text_rank[idx])) + (1.0 / (k + meta_rank[idx]))
            rescored.append(enriched)
    else:
        for hit, semantic, text_score, meta in zip(hits, semantic_scores, text_scores, meta_scores):
            enriched = dict(hit)
            enriched["_rerank_score"] = (0.60 * semantic) + (0.30 * max(text_score, meta)) + (0.10 * meta)
            rescored.append(enriched)

    rescored.sort(key=lambda hit: float(hit.get("_rerank_score", 0.0)), reverse=True)
    return rescored


def delete_single_chunk_from_marqo(
    document_id: str,
    chunk_num: int,
    index_name: str = "documents-index",
    workflow_id: Optional[str] = None,
) -> dict:
    """Delete a single chunk from Marqo, scoped to one document's own run.

    Thin pass-through to :meth:`pipeline.vector_store.VectorStore.delete_chunk`;
    the scoping, the capability probe and the fail-closed refusals live there.
    """
    return get_vector_store().delete_chunk(
        document_id, chunk_num, index_name, workflow_id=workflow_id
    )


def delete_chunks_from_marqo(
    document_id: str,
    index_name: str = "documents-index",
    workflow_id: Optional[str] = None,
) -> dict:
    """Delete all chunks for a document from Marqo — and only that document's.

    Thin pass-through to
    :meth:`pipeline.vector_store.VectorStore.delete_document`.
    """
    return get_vector_store().delete_document(
        document_id, index_name, workflow_id=workflow_id
    )


# =============================================================================
# Document helpers
# =============================================================================


def _build_document_detail(doc: dict) -> DocumentDetail:
    workflow_id = doc["workflow_id"]
    current_job = db.get_latest_document_job(workflow_id)
    return DocumentDetail(
        document_id=doc["document_id"],
        canonical_document_id=doc.get("canonical_document_id"),
        workflow_id=workflow_id,
        filename=doc["filename"],
        display_name=doc.get("display_name"),
        source_filename=doc.get("source_filename"),
        source_manifest_name=doc.get("source_manifest_name"),
        source_file_fingerprint=doc.get("source_file_fingerprint"),
        authoritative=bool(doc.get("source_manifest_name")),
        instance=normalize_instance(doc.get("instance")),
        filepath=doc["filepath"],
        stage=DocumentStage(doc["stage"]),
        page_count=doc.get("page_count", 0),
        chunk_count=doc.get("chunk_count", 0),
        error_message=doc.get("error_message"),
        reindex_required=bool(doc.get("reindex_required")),
        reindex_reason=doc.get("reindex_reason"),
        available_actions=_list_available_actions(doc, current_job),
        translated_count=sum(1 for p in db.get_pages(workflow_id) if p.get("translated_markdown")),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
        ocr_completed_at=doc.get("ocr_completed_at"),
        translation_completed_at=doc.get("translation_completed_at"),
        chunks_completed_at=doc.get("chunks_completed_at"),
        ingested_at=doc.get("ingested_at"),
        source_type=doc.get("source_type"),
        canonical_input_type=doc.get("canonical_input_type"),
        stop_after_ocr=bool(doc.get("stop_after_ocr")),
        original_artifact_id=doc.get("original_artifact_id"),
        normalized_artifact_id=doc.get("normalized_artifact_id"),
        latest_job_id=doc.get("latest_job_id"),
        current_job=current_job,
        artifacts=db.list_document_artifacts(workflow_id),
        index_status=db.list_document_index_status(workflow_id),
    )


def _build_stage_io_payload(workflow_id: str, current_stage: Optional[str] = None) -> dict:
    artifacts = db.list_document_artifacts(workflow_id)
    grouped: dict[str, dict] = {}
    for stage_id, label, description in PIPELINE_STAGES:
        grouped[stage_id] = {
            "stage": stage_id,
            "label": label,
            "description": description,
            "input_artifacts": [],
            "output_artifacts": [],
        }

    input_types = {"original_upload", "normalized_pdf", "normalized_spreadsheet"}
    for artifact in artifacts:
        stage = artifact.get("stage") or "registered"
        if stage not in grouped:
            grouped[stage] = {
                "stage": stage,
                "label": stage.replace("_", " ").title(),
                "description": "",
                "input_artifacts": [],
                "output_artifacts": [],
            }
        bucket = "input_artifacts" if artifact["artifact_type"] in input_types else "output_artifacts"
        grouped[stage][bucket].append(artifact)

    return {
        "workflow_id": workflow_id,
        "current_stage": current_stage,
        "stages": list(grouped.values()),
    }


async def _get_runtime_payload(workflow_id: str, doc: Optional[dict] = None) -> dict:
    doc = doc or db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")
    current_job = db.get_latest_document_job(workflow_id)
    runtime_workflow_id = (
        current_job.get("temporal_workflow_id")
        if current_job and current_job.get("status") == "running" and current_job.get("temporal_workflow_id")
        else workflow_id
    )

    chunking_progress = None
    if current_job and current_job.get("config_json"):
        try:
            parsed_config = json.loads(current_job["config_json"]) if isinstance(current_job["config_json"], str) else current_job["config_json"]
            if isinstance(parsed_config, dict):
                chunking_progress = parsed_config.get("chunking_progress")
        except Exception:
            chunking_progress = None

    # Reporting endpoint: a Temporal outage degrades the payload, never 500s it.
    tclient = await _temporal_client_or_none()

    runtime = {
        "workflow_id": workflow_id,
        "sqlite_stage": doc.get("stage"),
        "sqlite_error_message": doc.get("error_message"),
        "temporal_connected": tclient is not None,
        "job": current_job,
        "chunking_progress": chunking_progress,
        "temporal": None,
    }

    if tclient is None:
        return runtime

    try:
        handle = tclient.get_workflow_handle(runtime_workflow_id)
        description = await handle.describe()
        temporal_state = None
        query_error = None
        try:
            temporal_state = await handle.query("get_state")
        except Exception as exc:
            query_error = str(exc)

        runtime["temporal"] = {
            "workflow_id": runtime_workflow_id,
            "run_id": description.run_id,
            "status": description.status.name,
            "close_time": description.close_time.isoformat() if description.close_time else None,
            "execution_time": description.execution_time.isoformat() if description.execution_time else None,
            "state": temporal_state,
            "query_error": query_error,
        }
    except Exception as exc:
        runtime["temporal"] = {
            "workflow_id": workflow_id,
            "status": "UNAVAILABLE",
            "error": str(exc),
        }

    return runtime


async def _reconcile_single_document(doc: dict) -> dict:
    workflow_id = doc.get("workflow_id")
    current_stage = doc.get("stage")
    materialized = db.reconcile_materialized_state(workflow_id)
    if materialized and materialized.get("updated"):
        doc = db.get_document(workflow_id) or doc
        current_stage = doc.get("stage")
        return {
            "workflow_id": workflow_id,
            "action": "materialized_state_reconciled",
            "to": current_stage,
            "page_count": doc.get("page_count", 0),
            "chunk_count": doc.get("chunk_count", 0),
            "job_status": materialized.get("job_status"),
            "job_stage": materialized.get("job_stage"),
        }

    current_job = db.get_latest_document_job(workflow_id)
    runtime_workflow_id = (
        current_job.get("temporal_workflow_id")
        if current_job and current_job.get("status") == "running" and current_job.get("temporal_workflow_id")
        else workflow_id
    )

    try:
        handle = (await get_temporal_client()).get_workflow_handle(runtime_workflow_id)
        state = await asyncio.wait_for(
            handle.query("get_state"),
            timeout=5.0,
        )
        temporal_stage = state.get("stage") if state else None
        if temporal_stage and temporal_stage != current_stage:
            db.update_document_stage(workflow_id, temporal_stage)
            return {
                "workflow_id": workflow_id,
                "action": "stage_synced",
                "from": current_stage,
                "to": temporal_stage,
                "temporal_workflow_id": runtime_workflow_id,
            }
        return {
            "workflow_id": workflow_id,
            "action": "no_change",
            "stage": current_stage,
            "temporal_workflow_id": runtime_workflow_id,
        }
    except asyncio.TimeoutError:
        db.update_document_stage(workflow_id, "failed", error_message="Workflow query timed out")
        return {
            "workflow_id": workflow_id,
            "action": "marked_failed",
            "from": current_stage,
            "reason": "query_timeout",
        }
    except Exception as exc:
        error_msg = str(exc)
        if "not found" in error_msg.lower() or "workflow task" in error_msg.lower():
            db.update_document_stage(workflow_id, "failed", error_message="Workflow terminated or lost")
            db.log_audit(
                workflow_id=workflow_id,
                document_id=doc.get("document_id", ""),
                action_type="reconcile_failed",
                metadata={"from_stage": current_stage, "reason": "workflow_not_found"},
            )
            return {
                "workflow_id": workflow_id,
                "action": "marked_failed",
                "from": current_stage,
                "reason": "workflow_not_found",
            }
        return {
            "workflow_id": workflow_id,
            "action": "error",
            "from": current_stage,
            "reason": error_msg,
        }


# =============================================================================
# Approval helpers
# =============================================================================

async def _validate_approval_stage(workflow_id: str, expected_stage: str):
    """Validate that workflow is in the expected stage before approval."""
    try:
        handle = (await get_temporal_client()).get_workflow_handle(workflow_id)
        state = await handle.query("get_state")
        current_stage = state.get("stage") if isinstance(state, dict) else getattr(state, "stage", None)
        if current_stage != expected_stage:
            raise HTTPException(
                400,
                f"Cannot approve: workflow is in '{current_stage}' stage, expected '{expected_stage}'"
            )
        return handle
    except HTTPException:
        raise
    except Exception as e:
        # Try SQLite fallback to check if workflow exists but is completed/failed
        doc = db.get_document(workflow_id)
        if doc:
            raise HTTPException(
                400,
                f"Cannot approve: workflow is in '{doc.get('stage')}' stage (completed/failed workflows cannot be approved)"
            )
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


async def _execute_bulk_approval_action(
    request: BulkWorkflowActionRequest,
    action: str,
    expected_stage: str,
    signal_method,
    user: AuthUser,
) -> BulkWorkflowActionResponse:
    results: list[BulkWorkflowActionResult] = []
    for workflow_id in request.workflow_ids:
        doc = _document_for_user_or_none(workflow_id, user, permission=Permission.REVIEW)
        if not doc:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=False,
                action=action,
                message="document_not_found",
            ))
            continue
        current_stage = doc.get("stage")
        if current_stage != expected_stage:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=False,
                action=action,
                message=f"invalid_stage:{current_stage}",
            ))
            continue
        if request.dry_run:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=True,
                action=action,
                message="would_execute",
            ))
            continue
        try:
            handle = await _validate_approval_stage(workflow_id, expected_stage)
            await handle.signal(signal_method)
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=True,
                action=action,
                message="queued",
            ))
        except Exception as exc:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=False,
                action=action,
                message=str(exc),
            ))

    return BulkWorkflowActionResponse(
        action=action,
        dry_run=request.dry_run,
        requested=len(request.workflow_ids),
        succeeded=sum(1 for result in results if result.ok),
        failed=sum(1 for result in results if not result.ok),
        results=results,
    )


# Cap bulk auto-tag so one HTTP request cannot pin the API/LLM for hours.
BULK_AUTO_TAG_MAX_DOCS = 25
BULK_AUTO_TAG_CONCURRENCY = 2


# =============================================================================
# Audit log helper
# =============================================================================

def _log_audit(
    workflow_id: str,
    action_type: str,
    entity_type: str = None,
    entity_id: int = None,
    field_name: str = None,
    old_value = None,
    new_value = None,
    metadata: dict = None
):
    """
    Helper to log audit entries with JSON serialization.

    Args:
        workflow_id: The Temporal workflow ID
        action_type: Type of action (page_edit, chunk_edit, approval, etc.)
        entity_type: Type of entity (page, chunk)
        entity_id: Entity identifier (page_number, chunk_number)
        field_name: Name of the field changed
        old_value: Previous value (will be JSON serialized if not string)
        new_value: New value (will be JSON serialized if not string)
        metadata: Additional context as dict
    """
    # Get document_id from SQLite
    doc = db.get_document(workflow_id)
    document_id = doc["document_id"] if doc else workflow_id

    # Serialize values to JSON if needed
    old_str = json.dumps(old_value) if old_value is not None and not isinstance(old_value, str) else old_value
    new_str = json.dumps(new_value) if new_value is not None and not isinstance(new_value, str) else new_value

    db.log_audit(
        workflow_id=workflow_id,
        document_id=document_id,
        action_type=action_type,
        entity_type=entity_type,
        entity_id=entity_id,
        field_name=field_name,
        old_value=old_str,
        new_value=new_str,
        metadata=metadata
    )


# =============================================================================
# Chunk helpers
# =============================================================================


async def _auto_tag_document_chunks_impl(workflow_id: str, doc: dict) -> dict:
    """Run domain auto-tagging for every chunk in one document.

    Shared by the per-doc route and bulk auto-tag. Replaces ``source=auto`` tags
    only; manual tags are left intact. Caller must already have authorized access.
    """
    from .domain_tags.base import load_taxonomy_for_instance
    from .domain_tags.gemma_tagger import auto_tag_chunks
    from .domain_tags.service import get_domain_tagger, load_domain_tagging_config

    config = load_domain_tagging_config()
    if not config.enabled:
        raise HTTPException(400, "Domain tagging is disabled (DOMAIN_TAGGING_ENABLED=false)")

    chunks = db.get_chunks(workflow_id, include_excluded=True)
    if not chunks:
        raise HTTPException(400, "No chunks available for tagging")

    doc_context = " | ".join(
        part for part in [doc.get("source_manifest_name"), doc.get("display_name")] if part
    )
    taxonomy = load_taxonomy_for_instance(doc.get("instance"))
    tagger = get_domain_tagger(config)
    tagged_map = await auto_tag_chunks(
        chunks,
        filename=doc.get("filename") or "",
        doc_context=doc_context,
        tagger=tagger,
        taxonomy=taxonomy,
    )
    db.delete_auto_chunk_tags(workflow_id)
    tagged_chunks = 0
    total_tags = 0
    for chunk_num, tags in tagged_map.items():
        if not tags:
            continue
        db.replace_chunk_tags(
            workflow_id,
            chunk_num,
            [{"dimension": t.dimension, "value": t.value} for t in tags],
            source="auto",
        )
        tagged_chunks += 1
        total_tags += len(tags)

    if tagged_chunks:
        _mark_reindex_required(
            workflow_id,
            "Auto domain tags updated; search index is out of sync",
            metadata={"tagged_chunks": tagged_chunks},
        )

    return {
        "workflow_id": workflow_id,
        "tagged_chunks": tagged_chunks,
        "total_tags": total_tags,
    }


def _resolve_taxonomy_read_instance(user: AuthUser, instance: Optional[str]) -> str:
    """Pick the tenant whose taxonomy a caller reads.

    * an explicit ``instance`` is honoured after an access check (404 if the
      restricted caller may not see it — the tenant is hidden, not refused);
    * otherwise a data-unrestricted caller (local bypass) reads the default
      tenant; a single-tenant member reads its one tenant; a multi-tenant member
      reads the default tenant when it is in reach, else its first tenant.
    """
    if instance:
        return assert_instance_access(user, instance)
    allowed = allowed_instances(user)
    if allowed is None:
        return default_instance()
    # A token can carry a SEARCH-granting role with no tenant at all (a realm
    # role and no groups / tenant_roles / instances claim). It passes the
    # any-instance permission gate but has no tenant to resolve — 403 rather than
    # an IndexError -> 500 on the empty set.
    if not allowed:
        raise HTTPException(403, "No tenant is associated with this account")
    if len(allowed) == 1:
        return next(iter(allowed))
    default = default_instance()
    return default if default in allowed else sorted(allowed)[0]


# =============================================================================
# PDF serving
# =============================================================================

def _inline_content_disposition(filename: str) -> str:
    """Build a latin-1-safe Content-Disposition header for inline file display."""
    # Strip CR/LF/NUL so a crafted filename cannot inject response headers.
    safe_name = (filename or "document.pdf").replace('"', "'")
    safe_name = "".join(ch for ch in safe_name if ch not in "\r\n\0").strip() or "document.pdf"
    try:
        safe_name.encode("latin-1")
        return f'inline; filename="{safe_name}"'
    except UnicodeEncodeError:
        ascii_name = safe_name.encode("ascii", "ignore").decode("ascii").strip() or "document.pdf"
        encoded_name = quote(safe_name)
        return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"


# =============================================================================
# Index management (Phase 5) — many indexes per tenant, self-service
# =============================================================================


def _index_row_response(row: dict) -> dict:
    """Shape a tenant_indexes row for API responses."""
    return {
        "instance": row.get("instance"),
        "name": row.get("name"),
        "marqo_index": row.get("marqo_index"),
        "embedding_model": row.get("embedding_model"),
        "is_default": bool(row.get("is_default")),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
    }


# =============================================================================
# Tenant management (Phase 5) — app-side registry; gated master_admin
# =============================================================================


def reconcile_tenants(include_keycloak: bool = True) -> list[dict]:
    """Backfill ``tenants`` registry rows for tenants that exist de-facto.

    A tenant exists de-facto — without ever having a ``tenants`` row — as soon as
    it owns documents (``documents.instance``), an index (``tenant_indexes``) or a
    Keycloak Organization. Historically the registry was only populated by
    ``POST /tenants``, so those pre-existing tenants (e.g. the legacy default
    tenant carrying all its documents) never got a row and were invisible to the
    superadmin *Tenants* view.

    This reconciles that gap: it unions :func:`db.list_known_instances` (the
    local source of truth — documents + index registry) with the instances of
    every Keycloak Organization (best-effort; when KC admin is unconfigured or
    unreachable the identity-plane lookup is skipped and reconcile proceeds with
    just the local set). For every instance in the union lacking a ``tenants``
    row it inserts one via :func:`db.create_tenant_row` (idempotent — an existing
    row's ``display_name`` / ``status`` is never overwritten).

    It is **registry-only and non-destructive**: it never creates Marqo indexes
    or Keycloak objects and never mutates existing rows. Returns the current
    :func:`db.list_tenants`.
    """
    # instance -> preferred display name (KC org name, else the instance id).
    instances: dict[str, Optional[str]] = {}
    for inst in db.list_known_instances():
        instances.setdefault(inst, None)

    if include_keycloak:
        try:
            for org in keycloak_admin.list_organizations():
                inst = (org.get("instance") or org.get("name") or "").strip().lower()
                if not inst:
                    continue
                instances[inst] = org.get("name") or instances.get(inst)
        except (KeycloakAdminError, KeycloakAdminUnconfigured) as exc:
            # KC admin unconfigured / unreachable -> reconcile the local set only.
            logging.debug("reconcile_tenants: skipping Keycloak orgs: %s", exc)
        except Exception as exc:  # noqa: BLE001 - identity plane must never block reconcile
            logging.debug("reconcile_tenants: Keycloak org lookup failed: %s", exc)

    for inst, display_name in instances.items():
        if not db.get_tenant(inst):
            db.create_tenant_row(inst, display_name=display_name or inst)

    return db.list_tenants()


def _instance_has_kc_org(instance: str) -> bool:
    """Best-effort check for an existing Keycloak Organization for ``instance``.

    Any Keycloak error (including unconfigured admin) is swallowed as "no org" so
    the adopt decision degrades to the purely local signals.
    """
    inst = (instance or "").strip().lower()
    try:
        for org in keycloak_admin.list_organizations():
            candidates = {
                (org.get("instance") or "").strip().lower(),
                (org.get("name") or "").strip().lower(),
                (org.get("alias") or "").strip().lower(),
            }
            if inst in candidates:
                return True
    except (KeycloakAdminError, KeycloakAdminUnconfigured):
        return False
    except Exception:  # noqa: BLE001 - identity plane is optional here
        return False
    return False


def _provision_tenant_identity(
    instance: str, display_name: Optional[str]
) -> tuple[Optional[dict], Optional[str]]:
    """Best-effort Keycloak Organization + group tree provisioning (idempotent).

    Returns ``(keycloak_result, warning)``: on success ``keycloak_result`` holds
    the org id + group paths and ``warning`` is ``None``; when KC admin is
    unconfigured or a call fails, ``keycloak_result`` is ``None`` and ``warning``
    explains what was skipped. Never raises.
    """
    try:
        org_id = keycloak_admin.ensure_organization(instance, display_name=display_name)
        groups = keycloak_admin.ensure_group_tree(instance)
        return {"organization_id": org_id, "groups": sorted(groups.keys())}, None
    except KeycloakAdminUnconfigured as exc:
        return None, "Tenant created without Keycloak provisioning: " + str(exc)
    except KeycloakAdminError as exc:
        return None, f"Tenant created but Keycloak provisioning failed: {exc}"


def _adopt_existing_tenant(
    instance: str, display_name: Optional[str], existing_default: Optional[dict]
) -> dict:
    """Adopt a tenant that already exists de-facto into a clean, complete state.

    Ensures the ``tenants`` row (idempotent — never clobbers an existing
    display_name/status) and the Keycloak org/groups exist, and adopts the
    tenant's existing default index rather than creating a duplicate. Only when
    the tenant has no index at all (it existed solely via a registry row / KC
    org) is a default index provisioned. Returns the tenant + its default index
    with ``adopted: True``.
    """
    db.create_tenant_row(instance, display_name=display_name or instance)

    default_row = existing_default or db.get_default_index(instance)
    if default_row is None:
        # Known tenant with no index yet -> provision its default (with the same
        # cross-tenant physical-name collision guard as the new-tenant path).
        default_marqo_index = _new_marqo_index_name(instance, "default")
        if db.get_index_by_marqo_index(default_marqo_index):
            raise HTTPException(409, f"Physical index '{default_marqo_index}' already registered")
        try:
            _create_marqo_index_with_schema(default_marqo_index)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"Failed to create default Marqo index: {exc}") from exc
        default_row = db.create_index_row(
            instance=instance, name="default", marqo_index=default_marqo_index, is_default=True
        )

    keycloak_result, warning = _provision_tenant_identity(instance, display_name)
    response = {
        "tenant": db.get_tenant(instance),
        "default_index": _index_row_response(default_row) if default_row else None,
        "keycloak": keycloak_result,
        "adopted": True,
    }
    if warning:
        response["warning"] = warning
    return response


def _kc_unconfigured_503(exc: KeycloakAdminUnconfigured) -> HTTPException:
    """Translate an inert-KC-admin error into a helpful 503."""
    return HTTPException(
        503,
        "Keycloak admin is not configured on this server, so tenant user "
        "management is unavailable. Set KEYCLOAK_ADMIN_CLIENT_SECRET (and "
        "KEYCLOAK_ADMIN_CLIENT_ID / KEYCLOAK_ADMIN_BASE_URL / KEYCLOAK_REALM) "
        "to enable it.",
    )


def _assert_can_manage_members(user: AuthUser, instance: str) -> str:
    """Gate tenant member management: caller must manage users *in* the tenant.

    Mirrors the tenant view/manage index guards' 404-hide / 403-wrong-role shape,
    but with the platform admin **allowed everywhere** (member provisioning is a
    control-plane-adjacent operation the ``master_admin`` retains):

    * platform admin (``master_admin`` / local bypass) — allowed on any *known*
      tenant; an unknown tenant is still 404 (it owns the registry, no leak risk).
    * a tenant member holding ``manage_users`` in that tenant (i.e. its ``admin``)
      — allowed for THAT tenant only.
    * a member with an insufficient role (``viewer`` / ``content_curator``) — 403.
    * any other caller / cross-tenant access — 404 (never leak tenant existence).

    Returns the normalized instance id.
    """
    inst = normalize_instance(instance)
    known = db.get_tenant(inst) is not None
    if user.is_platform_admin:
        if not known:
            raise HTTPException(404, "Tenant not found")
        return inst
    # Non-platform callers must be able to reach the tenant AND it must exist;
    # both failures collapse to 404 so tenant existence is never leaked.
    if not known or not user_can_access_instance(user, inst):
        raise HTTPException(404, "Tenant not found")
    if Permission.MANAGE_USERS not in user.permissions_in(inst):
        raise HTTPException(403, "Managing a tenant's members requires admin in that tenant")
    return inst


def _kc_member_error_502(exc: KeycloakAdminError, operation: str) -> HTTPException:
    """Log the raw Keycloak failure, return a generic 502 to the caller.

    A tenant admin is not infrastructure staff: ``_admin_call`` errors embed the
    in-cluster admin URL, realm name and Keycloak internals, so the detail stays
    in the server log and the response says only which operation failed.
    """
    logging.error("Keycloak %s failed: %s", operation, exc)
    return HTTPException(502, f"Keycloak {operation} failed. See server logs for details.")


def _assert_not_self(user: AuthUser, user_id: str, action: str) -> None:
    """Refuse a member mutation the caller aimed at its own account.

    A tenant admin demoting or removing itself has no way back: the tenant loses
    the caller's ``manage_users`` mid-request and only a platform admin can undo it.
    """
    if user_id and user.user_id and user_id == user.user_id:
        raise HTTPException(403, f"You cannot {action} your own tenant membership")


def _assert_not_last_admin(user: AuthUser, instance: str, user_id: str, action: str) -> None:
    """Refuse a mutation that would leave ``instance`` with no ``admin`` member.

    Without this, removing (or demoting) the tenant's only admin leaves nobody
    holding ``manage_users`` in it. A platform admin is exempt — it manages the
    registry and can always re-provision an admin.
    """
    if user.is_platform_admin:
        return
    try:
        members = keycloak_admin.list_members(instance)
    except KeycloakAdminUnconfigured as exc:
        raise _kc_unconfigured_503(exc) from exc
    except KeycloakAdminError as exc:
        raise _kc_member_error_502(exc, "member listing") from exc
    admins = [m for m in members if "admin" in (m.get("roles") or [])]
    if len(admins) == 1 and admins[0].get("user_id") == user_id:
        raise HTTPException(
            409,
            f"Cannot {action} the tenant's only admin — promote another member to "
            "admin first",
        )


# ---------------------------------------------------------------------------
# Per-tenant tag taxonomy management (Phase 5)
# ---------------------------------------------------------------------------


def _ensure_tenant_taxonomy_seeded(instance: str) -> None:
    """Seed a tenant's taxonomy from the shipped default the first time it is
    managed, so an admin edits a populated copy (never an empty one). Idempotent
    on the seed marker: a tenant seeded once is left untouched forever after, so
    an admin who deleted every node keeps an empty taxonomy."""
    from .domain_tags.base import load_taxonomy

    try:
        db.seed_taxonomy_for_instance(instance, load_taxonomy())
    except Exception as exc:  # noqa: BLE001 - seeding must not break a management call
        logging.warning("Taxonomy seed for %s failed (non-fatal): %s", instance, exc)


def _tenant_taxonomy_payload(instance: str) -> dict:
    """The tenant's own taxonomy, honouring a deliberately EMPTY one.

    A seeded tenant always answers with its own rows — ``{domains: {}}`` when the
    admin removed them all. Only a tenant that could not be seeded at all falls
    back to the shipped file default (the loader's transitional behaviour).
    """
    if db.taxonomy_is_seeded(instance):
        return db.get_taxonomy(instance) or {"instance": instance, "domains": {}}
    from .domain_tags.service import get_taxonomy_for_api

    return get_taxonomy_for_api(instance)


# =============================================================================
# Route handlers
#
# They live in `pipeline.routers` and are re-exported here so that `api.<handler>`
# keeps resolving to the same function object callers already address.
# =============================================================================

from .routers.documents import (  # noqa: E402,F401
    disable_document,
    get_document,
    get_document_allowed_actions,
    get_document_artifact,
    get_document_artifact_content,
    get_document_audit_log,
    get_document_cohorts,
    get_document_graph,
    get_document_pdf,
    get_document_runtime,
    get_document_stage_io,
    get_documents_summary,
    get_workflow_error_details,
    list_document_artifacts,
    list_document_jobs,
    list_documents,
    restore_document,
    set_document_demo,
    set_document_query_enabled,
    start_batch_workflows,
    start_document_workflow,
    update_document_metadata,
    upload_and_process,
)
from .routers.documents_actions import (  # noqa: E402,F401
    approve_chunks,
    approve_ingestion,
    approve_ocr,
    approve_translation,
    auto_tag_document_chunks,
    bulk_approve_chunks,
    bulk_approve_ocr,
    bulk_approve_translation,
    bulk_auto_tag_documents,
    bulk_reindex_documents,
    clear_reindex_required,
    mark_reindex_required,
    reconcile_document_states,
    reconcile_single_document,
    reingest_document,
    retry_chunking,
    retry_ingestion,
    retry_ocr,
    retry_translation,
)
from .routers.content import (  # noqa: E402,F401
    delete_chunk,
    export_chunks,
    export_markdown,
    get_chunk,
    get_document_marqo_status,
    get_page,
    list_chunks,
    list_document_marqo_chunks,
    list_pages,
    reset_chunk,
    reset_page,
    resolve_provenance_chunk,
    search_chunks_across_documents,
    set_chunk_tags,
    update_chunk,
    update_page,
)
from .routers.search import (  # noqa: E402,F401
    get_domain_tag_taxonomy,
    get_marqo_index_settings,
    get_marqo_index_stats,
    get_marqo_indexes_summary,
    get_search_settings,
    get_search_settings_audit,
    reset_search_settings,
    run_marqo_search,
    update_search_settings_endpoint,
)
from .routers.tenants import (  # noqa: E402,F401
    change_tenant_member_role_route,
    create_tenant_admin_route,
    create_tenant_index,
    create_tenant_member_route,
    create_tenant_route,
    create_tenant_taxonomy_node_route,
    delete_tenant_index,
    delete_tenant_route,
    delete_tenant_taxonomy_dimension_route,
    delete_tenant_taxonomy_node_route,
    get_tenant_taxonomy_route,
    list_tenant_indexes,
    list_tenant_members_route,
    list_tenants_route,
    reconcile_tenants_route,
    remove_tenant_member_route,
    rename_tenant_taxonomy_node_route,
    reset_tenant_member_password_route,
    suspend_tenant_route,
)
from .routers.admin import (  # noqa: E402,F401
    auth_me,
    create_marqo_index,
    get_all_audit_logs,
    get_ingest_info,
    get_marqo_index_schema,
    get_operations_queue,
    get_pipeline_stages,
    get_run,
    health,
    list_runs,
)
