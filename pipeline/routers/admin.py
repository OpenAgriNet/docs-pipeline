"""Admin, ops, runs, audit, auth and health."""

from fastapi import APIRouter, HTTPException, Header, Query
from typing import Optional
from .. import api_support as support
from ..auth.deps import CurrentUser, RequireAdmin, RequirePlatformAdmin, RequireSearch
from ..auth.tenancy import user_can_access_instance
from ..models import (
    AuditLogResponse,
    OperationQueueEntry,
    OperationQueueResponse,
    PIPELINE_STAGES,
)
from ..vector_store import VectorStoreError

router = APIRouter()


@router.get("/auth/me")
async def auth_me(user: CurrentUser):
    """Return the authenticated caller (local bypass user when AUTH_DISABLED=true)."""
    return {
        "user_id": user.user_id,
        "username": user.username,
        "email": user.email,
        "roles": user.roles,
        "permissions": sorted(p.value for p in user.permissions),
        "instances": user.instances,
        "envs": user.envs,
        "auth_disabled": user.token_disabled_mode,
    }


@router.get("/operations/queue", response_model=OperationQueueResponse)
async def get_operations_queue(
    user: RequireSearch,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled"),
):
    """Return documents that currently need operator or agent action."""
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    rows, total = support.db.list_operations_queue(
        limit=limit,
        offset=offset,
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=support._instance_scope_for_user(user),
    )
    items = [
        OperationQueueEntry(
            workflow_id=row["workflow_id"],
            filename=row["filename"],
            stage=row["stage"],
            job_id=row.get("job_id"),
            job_type=row.get("job_type"),
            job_status=row.get("job_status"),
            started_at=row.get("started_at"),
            error_message=row.get("error_message") or row.get("reindex_reason"),
            available_actions=support._list_available_actions(row, row),
        )
        for row in rows
    ]
    return OperationQueueResponse(items=items, total=total)


@router.get("/runs")
async def list_runs(
    user: RequireSearch,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    status: Optional[str] = None,
):
    """List recent document jobs, scoped to the caller's accessible instances."""
    return support.db.list_runs(
        limit=limit,
        offset=offset,
        status=status,
        instances=support._instance_scope_for_user(user),
    )


@router.get("/runs/{job_id}")
async def get_run(job_id: int, user: RequireSearch):
    """Get a specific document job/run (scoped to the caller's instances)."""
    run = support.db.get_document_job(job_id)
    if not run:
        raise HTTPException(404, f"Run not found: {job_id}")
    # A restricted caller must never open another tenant's run. Resolve the
    # run's owning document and hide it (404) when out of the caller's scope.
    owner = support.db.get_document(run.get("workflow_id"))
    if not owner or not user_can_access_instance(user, owner.get("instance")):
        raise HTTPException(404, f"Run not found: {job_id}")
    return run


@router.get("/audit", response_model=AuditLogResponse)
async def get_all_audit_logs(
    user: RequireSearch,
    action_type: str = None,
    limit: int = Query(50, le=200),
    offset: int = 0
):
    """
    Get global audit trail across all documents.

    Returns a list of all changes including:
    - Stage transitions
    - Page edits
    - Chunk edits
    - Approvals
    - Resets

    Each entry includes the document filename for context.

    Scoped to the caller's accessible instances so a tenant caller never sees
    another tenant's audit trail. Only a data-unrestricted caller (local bypass)
    sees all; a control-plane ``master_admin`` has no data scope and sees none.
    """
    instances = support._instance_scope_for_user(user)
    logs = support.db.get_all_audit_logs(
        action_type=action_type,
        limit=limit,
        offset=offset,
        instances=instances,
    )
    total = support.db.get_all_audit_log_count(action_type, instances=instances)

    return AuditLogResponse(
        logs=logs,
        total=total,
        limit=limit,
        offset=offset
    )


@router.get("/health")
async def health():
    """Health check. Reports Temporal reachability; never fails on an outage."""
    return {
        "status": "ok",
        "temporal_connected": await support._temporal_client_or_none() is not None
    }


@router.get("/admin/index/schema")
async def get_marqo_index_schema(
    user: RequirePlatformAdmin,
    index_name: str = Query("documents-index", description="Marqo index name"),
):
    """Report whether the live Marqo index includes filterable domain_tags.

    Raw physical-index tool: it ignores tenant scoping and addresses any Marqo
    index by name, so it is restricted to the platform super-admin
    (``RequirePlatformAdmin``). A per-tenant ``admin`` must use the tenant-scoped
    index routes (``/tenants/{instance}/indexes*``) instead.
    """
    from ..vector_store import passage_schema_field_names

    store = support.get_vector_store()
    try:
        field_names = sorted(store.field_names(index_name))
    except VectorStoreError as exc:
        raise HTTPException(404, f"Index '{index_name}' not found: {exc}") from exc

    has_domain_tags_field = "domain_tags" in set(field_names)
    canonical_fields = sorted(passage_schema_field_names())
    missing_fields = sorted(set(canonical_fields) - set(field_names))

    return {
        "index_name": index_name,
        "marqo_url": store.url,
        "has_domain_tags_field": has_domain_tags_field,
        "fields": field_names,
        "canonical_passage_fields": canonical_fields,
        "missing_canonical_fields": missing_fields,
        "domain_tags_ready": has_domain_tags_field,
        "note": (
            "Structured Marqo indexes cannot add fields after creation. "
            "If domain_tags is missing, recreate the index with the passage schema "
            "and reingest documents to enable tag filtering in search."
        ),
    }


@router.post("/admin/index/create")
async def create_marqo_index(
    user: RequirePlatformAdmin,
    index_name: str = Query("documents-index", description="Marqo index name"),
    recreate_if_exists: bool = Query(False, description="If true, delete existing index and create with passage schema"),
):
    """
    Create the Marqo index with the passage schema (E5 text_for_embedding + full metadata).

    Use this to ensure the index exists with the correct schema before reingest, or to
    reset the index to the canonical schema. The Marqo endpoint comes from the environment.

    Raw physical-index tool restricted to the platform super-admin
    (``RequirePlatformAdmin``): it addresses any Marqo index by name and can
    ``delete_index`` it with ``recreate_if_exists=true``, so a per-tenant
    ``admin`` must NOT reach it (that would let one tenant destroy another
    tenant's — or the shared legacy — index). Tenant self-service index
    management lives under ``/tenants/{instance}/indexes*``.
    """
    _ = user
    from ..vector_store import passage_index_settings

    store = support.get_vector_store()
    settings = passage_index_settings()
    index_exists = store.index_exists(index_name)

    if index_exists and not recreate_if_exists:
        return {
            "index": index_name,
            "created": False,
            "message": "Index already exists. Use recreate_if_exists=true to replace with passage schema.",
        }

    if index_exists and recreate_if_exists:
        store.delete_index(index_name)

    store.create_index(index_name, settings)
    return {
        "index": index_name,
        "created": True,
        "message": "Index created with passage schema (text_for_embedding, full metadata).",
        "marqo_url": store.url,
    }


@router.get("/admin/ingest-info")
async def get_ingest_info(user: RequireAdmin):
    """
    Return what the running container's ingest code would send to Marqo.
    Use this to verify the API/worker image has the passage schema (text_for_embedding, etc.).
    """
    from ..activities import _prepare_records
    from ..vector_store import passage_schema_field_names

    passage_fields = sorted(passage_schema_field_names())
    has_text_for_embedding = "text_for_embedding" in set(passage_fields)
    # One fake chunk to see exact record shape the worker would send
    fake_chunk = {
        "chunk_number": 0,
        "original_text": "Sample text.",
        "edited_text": None,
        "is_excluded": False,
        "token_count": 2,
        "page_start": 1,
        "page_end": 1,
    }
    sample_records = _prepare_records(
        document_id="debug-document-id",
        filename="debug.pdf",
        chunks=[fake_chunk],
        workflow_id="doc-debugsample12",
    )
    sample_record_keys = sorted(sample_records[0].keys()) if sample_records else []
    return {
        "passage_schema_fields": passage_fields,
        "has_text_for_embedding": has_text_for_embedding,
        "sample_record_keys": sample_record_keys,
        "sample_has_passage_prefix": (
            sample_records[0].get("text_for_embedding", "").startswith("passage:")
            if sample_records else False
        ),
    }


@router.get("/pipeline/stages")
async def get_pipeline_stages(user: RequireSearch):
    """Get the pipeline stages for UI stepper display."""
    return [
        {"id": stage[0], "label": stage[1], "description": stage[2]}
        for stage in PIPELINE_STAGES
    ]
