"""Stage approvals, retries, bulk actions and reconcile."""

import asyncio
from datetime import datetime
from fastapi import APIRouter, HTTPException
from .. import db
from ..auth.deps import RequirePipeline, RequireReview
from ..auth.permissions import Permission
from ..models import (
    BulkWorkflowActionRequest,
    BulkWorkflowActionResponse,
    BulkWorkflowActionResult,
    ReindexStateRequest,
)
from ..services import access, documents as document_service, taxonomy, workflow_runtime

router = APIRouter()


def _details_json_to_dict(value) -> dict | None:
    if not value:
        return None
    if isinstance(value, dict):
        return value
    try:
        import json

        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


@router.post("/documents/{workflow_id}/reingest")
async def reingest_document(
    workflow_id: str,
    user: RequirePipeline,
    marqo_url: str = "",
    index_name: str = "documents-index",
):
    """
    Re-ingest a completed document to Marqo.

    Use this to re-ingest documents that completed but weren't properly
    indexed (e.g., due to index schema changes). This starts a lightweight
    workflow that uses chunks already stored in SQLite.

    The document must have chunks stored in SQLite (typically from a
    completed or previously ingested document).
    Client-supplied marqo_url is ignored; ingest resolves the endpoint from the environment.
    """
    marqo_url = ""
    # Get document from SQLite
    doc = access.require_document_for_user(workflow_id, user, permission=Permission.PIPELINE)
    if not document_service.reingest_allowed(doc):
        raise HTTPException(
            409,
            "Reingest is blocked while force OCR is rebuilding this document. "
            "Wait until chunking reaches review/ready/completed.",
        )

    # Get chunks from SQLite
    chunks = db.get_chunks(workflow_id, include_excluded=False)
    if not chunks:
        raise HTTPException(400, f"No chunks found for document. The document may need to be reprocessed from scratch.")

    document_id = doc.get("document_id", "")
    filename = doc.get("filename", "")
    page_count = doc.get("page_count", 0)

    # Generate unique workflow ID for re-ingestion
    import time
    reingest_workflow_id = f"{workflow_id}-reingest-{int(time.time())}"

    # Start re-ingestion workflow (tenant-tagged)
    await workflow_runtime.start_reingestion(
        args=[
            document_id,
            filename,
            workflow_id,  # original workflow_id for SQLite updates
            page_count,
            len(chunks),
            marqo_url,
            index_name
        ],
        id=reingest_workflow_id,
        instance=doc.get("instance"),
    )
    db.create_document_job(
        workflow_id=workflow_id,
        job_type="reingest",
        temporal_workflow_id=reingest_workflow_id,
        status="running",
        current_stage="ingesting",
        config={"index_name": index_name, "chunk_count": len(chunks), "marqo_url": marqo_url or None},
    )

    # Log audit
    db.log_audit(
        workflow_id=workflow_id,
        document_id=document_id,
        action_type="reingest_started",
        metadata={"reingest_workflow_id": reingest_workflow_id, "chunk_count": len(chunks)}
    )

    return {
        "workflow_id": workflow_id,
        "reingest_workflow_id": reingest_workflow_id,
        "chunk_count": len(chunks),
        "status": "started"
    }


@router.post("/documents/{workflow_id}/retry-ingestion")
async def retry_ingestion(
    workflow_id: str,
    user: RequirePipeline,
    marqo_url: str = "",
    index_name: str = "documents-index",
):
    """Alias for reingesting a document when search is stale or missing."""
    return await reingest_document(
        workflow_id,
        user=user,
        marqo_url="",
        index_name=index_name,
    )


@router.post("/documents/{workflow_id}/retry-ocr")
async def retry_ocr(
    workflow_id: str,
    user: RequirePipeline,
    force: bool = False,
    discard_edits: bool = False,
):
    """Retry OCR for an existing document and stop at OCR review.

    Default (``force=false``): resume — skip pages already persisted in SQLite
    (safe for crash recovery / Temporal retries).

    ``force=true``: clear this document's pages, re-run OCR, and write a new
    ``ocr_pages_json`` artifact (prior MinIO exports are kept). Operator OCR
    edits are preserved unless ``discard_edits=true``.
    """
    doc = access.require_document_for_user(workflow_id, user, permission=Permission.PIPELINE)
    filepath = doc.get("filepath")
    if not filepath:
        raise HTTPException(400, "Document has no source filepath for OCR retry")
    if discard_edits and not force:
        raise HTTPException(400, "discard_edits requires force=true")
    temporal_workflow_id = f"{workflow_id}-retry-ocr-{int(datetime.utcnow().timestamp())}"
    job_id = db.create_document_job(
        workflow_id=workflow_id,
        job_type="ocr_retry",
        temporal_workflow_id=temporal_workflow_id,
        status="running",
        current_stage="ocr_processing",
        config={
            "source": "api_retry_ocr",
            "force": force,
            "discard_edits": discard_edits,
        },
    )
    prior_index_rows: list[dict] = []
    prior_state = {
        "chunk_count": doc.get("chunk_count"),
        "translation_completed_at": doc.get("translation_completed_at"),
        "chunks_completed_at": doc.get("chunks_completed_at"),
        "ingested_at": doc.get("ingested_at"),
        "reindex_required": doc.get("reindex_required"),
        "reindex_reason": doc.get("reindex_reason"),
    }
    if force:
        now = datetime.utcnow().isoformat()
        prior_index_rows = db.list_document_index_status(workflow_id)
        db.update_document_fields(
            workflow_id,
            latest_job_id=job_id,
            error_message=None,
            chunk_count=0,
            translation_completed_at=None,
            chunks_completed_at=None,
            ingested_at=None,
            reindex_required=1,
            reindex_reason="force_ocr_requested",
        )
        for row in prior_index_rows:
            db.upsert_document_index_status(
                workflow_id=workflow_id,
                index_name=row.get("index_name"),
                marqo_doc_id=row.get("marqo_doc_id") or doc.get("document_id"),
                chunk_count_indexed=0,
                last_verified_at=now,
                schema_version=row.get("schema_version"),
                status="stale",
                details={
                    "reason": "force_ocr_requested",
                    "temporal_workflow_id": temporal_workflow_id,
                },
            )
    try:
        await workflow_runtime.start_ocr_retry(
            args=[
                workflow_id,
                doc["document_id"],
                doc["filename"],
                filepath,
                force,
                discard_edits,
                job_id,
            ],
            id=temporal_workflow_id,
            instance=doc.get("instance"),
        )
    except Exception as exc:
        if force:
            db.update_document_fields(
                workflow_id,
                latest_job_id=job_id,
                error_message=str(exc),
                chunk_count=prior_state.get("chunk_count"),
                translation_completed_at=prior_state.get("translation_completed_at"),
                chunks_completed_at=prior_state.get("chunks_completed_at"),
                ingested_at=prior_state.get("ingested_at"),
                reindex_required=prior_state.get("reindex_required"),
                reindex_reason=prior_state.get("reindex_reason"),
            )
            for row in prior_index_rows:
                db.upsert_document_index_status(
                    workflow_id=workflow_id,
                    index_name=row.get("index_name"),
                    marqo_doc_id=row.get("marqo_doc_id"),
                    chunk_count_indexed=row.get("chunk_count_indexed"),
                    last_indexed_at=row.get("last_indexed_at"),
                    last_verified_at=row.get("last_verified_at"),
                    schema_version=row.get("schema_version"),
                    status=row.get("status") or "unknown",
                    details=_details_json_to_dict(row.get("details_json")),
                )
        db.update_document_job(
            job_id,
            status="failed",
            current_stage="failed",
            error_message=str(exc),
            completed_at=datetime.utcnow().isoformat(),
        )
        if not force:
            db.update_document_fields(workflow_id, latest_job_id=job_id, error_message=str(exc))
        raise
    if force:
        now = datetime.utcnow().isoformat()
        if not prior_index_rows:
            resolved_index_name = db.resolve_ingest_index_name(
                doc.get("instance"),
                doc.get("index"),
            )
            db.upsert_document_index_status(
                workflow_id=workflow_id,
                index_name=resolved_index_name,
                marqo_doc_id=doc.get("document_id"),
                chunk_count_indexed=0,
                last_verified_at=now,
                status="stale",
                details={
                    "reason": "force_ocr_requested",
                    "temporal_workflow_id": temporal_workflow_id,
                },
            )
    else:
        db.update_document_fields(workflow_id, latest_job_id=job_id, error_message=None)
    action_type = "force_ocr" if force else "retry_ocr"
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", workflow_id),
        action_type=action_type,
        metadata={
            "actor": user.user_id,
            "temporal_workflow_id": temporal_workflow_id,
            "force": force,
            "discard_edits": discard_edits,
            # Force always replaces every page for the workflow; recorded
            # explicitly so the audit trail states the blast radius.
            "scope": "all_pages" if force else "resume_missing_pages",
        },
    )
    return {
        "workflow_id": workflow_id,
        "status": "started",
        "retry_workflow_id": temporal_workflow_id,
        "force": force,
        "discard_edits": discard_edits,
    }


@router.post("/documents/{workflow_id}/retry-translation")
async def retry_translation(
    workflow_id: str,
    user: RequirePipeline,
    force_retranslate: bool = False,
):
    """Retry translation for an existing document and stop at translation review."""
    doc = access.require_document_for_user(workflow_id, user, permission=Permission.PIPELINE)
    if not db.get_pages(workflow_id):
        raise HTTPException(400, "No OCR pages found for translation retry")
    temporal_workflow_id = f"{workflow_id}-retry-translation-{int(datetime.utcnow().timestamp())}"
    await workflow_runtime.start_translation_retry(
        args=[workflow_id, doc["document_id"], doc["filename"], force_retranslate],
        id=temporal_workflow_id,
        instance=doc.get("instance"),
    )
    job_id = db.create_document_job(
        workflow_id=workflow_id,
        job_type="translation_retry",
        temporal_workflow_id=temporal_workflow_id,
        status="running",
        current_stage="translation_processing",
        config={
            "source": "api_retry_translation",
            "force_retranslate": force_retranslate,
        },
    )
    db.update_document_fields(workflow_id, latest_job_id=job_id, error_message=None)
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", workflow_id),
        action_type="retry_translation",
        metadata={
            "temporal_workflow_id": temporal_workflow_id,
            "force_retranslate": force_retranslate,
        },
    )
    return {
        "workflow_id": workflow_id,
        "status": "started",
        "retry_workflow_id": temporal_workflow_id,
        "force_retranslate": force_retranslate,
    }


@router.post("/documents/{workflow_id}/retry-chunking")
async def retry_chunking(
    workflow_id: str,
    user: RequirePipeline,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
):
    """Retry chunking for an existing document and stop at chunk review."""
    doc = access.require_document_for_user(workflow_id, user, permission=Permission.PIPELINE)
    if not db.get_pages(workflow_id):
        raise HTTPException(400, "No page content found for chunking retry")
    temporal_workflow_id = f"{workflow_id}-retry-chunking-{int(datetime.utcnow().timestamp())}"
    await workflow_runtime.start_chunking_retry(
        args=[
            workflow_id,
            doc["document_id"],
            doc["filename"],
            doc.get("page_count", 0),
            chunk_size,
            chunk_overlap,
            min_tokens,
        ],
        id=temporal_workflow_id,
        instance=doc.get("instance"),
    )
    job_id = db.create_document_job(
        workflow_id=workflow_id,
        job_type="chunking_retry",
        temporal_workflow_id=temporal_workflow_id,
        status="running",
        current_stage="chunking",
        config={
            "source": "api_retry_chunking",
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "min_tokens": min_tokens,
        },
    )
    db.update_document_fields(workflow_id, latest_job_id=job_id, error_message=None)
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", workflow_id),
        action_type="retry_chunking",
        metadata={"temporal_workflow_id": temporal_workflow_id},
    )
    return {"workflow_id": workflow_id, "status": "started", "retry_workflow_id": temporal_workflow_id}


@router.post("/documents/{workflow_id}/mark-reindex-required")
async def mark_reindex_required(workflow_id: str, payload: ReindexStateRequest, user: RequirePipeline):
    """Mark a document as needing reindex after chunk edits or operational drift."""
    access.require_document_for_user(workflow_id, user, permission=Permission.PIPELINE)
    updated = document_service.mark_reindex_required(
        workflow_id,
        payload.reason or "Marked manually for reindex",
        metadata={"source": "api"},
    )
    return {
        "workflow_id": workflow_id,
        "reindex_required": bool(updated.get("reindex_required")) if updated else True,
        "reindex_reason": updated.get("reindex_reason") if updated else payload.reason,
    }


@router.post("/documents/{workflow_id}/clear-reindex-required")
async def clear_reindex_required(workflow_id: str, user: RequirePipeline):
    """Clear the reindex-required flag after verification or reingestion."""
    doc = access.require_document_for_user(workflow_id, user, permission=Permission.PIPELINE)
    old_reason = doc.get("reindex_reason")
    updated = db.mark_document_reindex_required(workflow_id, False)
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", workflow_id),
        action_type="clear_reindex_required",
        field_name="reindex_required",
        old_value="true",
        new_value="false",
        metadata={"reason": old_reason},
    )
    return {
        "workflow_id": workflow_id,
        "reindex_required": bool(updated.get("reindex_required")) if updated else False,
        "reindex_reason": updated.get("reindex_reason") if updated else None,
    }


@router.post("/documents/{workflow_id}/reconcile")
async def reconcile_single_document(workflow_id: str, user: RequirePipeline):
    """Reconcile SQLite stage with Temporal state for one document."""
    doc = access.require_document_for_user(workflow_id, user, permission=Permission.PIPELINE)
    return await workflow_runtime.reconcile_single_document(doc)


@router.post("/documents/bulk/approve-ocr", response_model=BulkWorkflowActionResponse)
async def bulk_approve_ocr(request: BulkWorkflowActionRequest, user: RequireReview):
    """Bulk-approve documents waiting in OCR review."""
    return await workflow_runtime.execute_bulk_approval_action(
        request,
        action="approve_ocr",
        expected_stage="ocr_review",
        approval="ocr",
        user=user,
    )


@router.post("/documents/bulk/approve-translation", response_model=BulkWorkflowActionResponse)
async def bulk_approve_translation(request: BulkWorkflowActionRequest, user: RequireReview):
    """Bulk-approve documents waiting in translation review."""
    return await workflow_runtime.execute_bulk_approval_action(
        request,
        action="approve_translation",
        expected_stage="translation_review",
        approval="translation",
        user=user,
    )


@router.post("/documents/bulk/approve-chunks", response_model=BulkWorkflowActionResponse)
async def bulk_approve_chunks(request: BulkWorkflowActionRequest, user: RequireReview):
    """Bulk-approve documents waiting in chunk review."""
    return await workflow_runtime.execute_bulk_approval_action(
        request,
        action="approve_chunks",
        expected_stage="chunk_review",
        approval="chunks",
        user=user,
    )


@router.post("/documents/bulk/reindex", response_model=BulkWorkflowActionResponse)
async def bulk_reindex_documents(
    request: BulkWorkflowActionRequest,
    user: RequirePipeline,
    marqo_url: str = "",
    index_name: str = "documents-index",
):
    """Bulk queue reingestion for completed or dirty documents.

    Client-supplied marqo_url is ignored; ingest resolves the endpoint from the environment.
    """
    marqo_url = ""
    results: list[BulkWorkflowActionResult] = []
    for workflow_id in request.workflow_ids:
        doc = access.document_for_user_or_none(workflow_id, user, permission=Permission.PIPELINE)
        if not doc:
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=False, action="reindex", message="document_not_found"))
            continue
        if doc.get("stage") not in {"completed", "ready_for_ingestion", "chunk_review"} and not doc.get("reindex_required"):
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=False, action="reindex", message=f"invalid_stage:{doc.get('stage')}"))
            continue
        if request.dry_run:
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=True, action="reindex", message="would_execute"))
            continue
        try:
            await reingest_document(workflow_id, user=user, marqo_url=marqo_url, index_name=index_name)
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=True, action="reindex", message="queued"))
        except Exception as exc:
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=False, action="reindex", message=str(exc)))

    return BulkWorkflowActionResponse(
        action="reindex",
        dry_run=request.dry_run,
        requested=len(request.workflow_ids),
        succeeded=sum(1 for result in results if result.ok),
        failed=sum(1 for result in results if not result.ok),
        results=results,
    )


@router.post("/documents/bulk/auto-tag", response_model=BulkWorkflowActionResponse)
async def bulk_auto_tag_documents(request: BulkWorkflowActionRequest, user: RequireReview):
    """Auto-tag all chunks for each selected document (same logic as per-doc auto-tag).

    Per-document failures are returned in ``results`` and do not abort the batch.
    Cross-tenant / missing ids become ``document_not_found`` (no existence leak).
    Soft-deleted docs are skipped. Manual chunk tags are preserved (only ``auto``
    tags are replaced), matching ``POST /documents/{id}/auto-tag-chunks``.
    """
    from ..domain_tags.service import load_domain_tagging_config

    if len(request.workflow_ids) > taxonomy.BULK_AUTO_TAG_MAX_DOCS:
        raise HTTPException(
            400,
            f"Too many documents (max {taxonomy.BULK_AUTO_TAG_MAX_DOCS} per bulk auto-tag request)",
        )
    if not request.workflow_ids:
        raise HTTPException(400, "workflow_ids must not be empty")

    # Dedup while preserving caller order — duplicate ids would otherwise run
    # redundant concurrent passes over the same document.
    workflow_ids = list(dict.fromkeys(request.workflow_ids))

    config = load_domain_tagging_config()
    if not config.enabled:
        raise HTTPException(400, "Domain tagging is disabled (DOMAIN_TAGGING_ENABLED=false)")

    action = "auto_tag"
    results: list[BulkWorkflowActionResult] = []

    async def _one(workflow_id: str) -> BulkWorkflowActionResult:
        doc = access.document_for_user_or_none(workflow_id, user, permission=Permission.REVIEW)
        if not doc:
            return BulkWorkflowActionResult(
                workflow_id=workflow_id, ok=False, action=action, message="document_not_found"
            )
        if doc.get("is_disabled"):
            return BulkWorkflowActionResult(
                workflow_id=workflow_id, ok=False, action=action, message="document_disabled"
            )
        chunks = db.get_chunks(workflow_id, include_excluded=True)
        if not chunks:
            return BulkWorkflowActionResult(
                workflow_id=workflow_id, ok=False, action=action, message="no_chunks"
            )
        if request.dry_run:
            return BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=True,
                action=action,
                message=f"would_execute:{len(chunks)}_chunks",
            )
        try:
            tagged = await taxonomy.auto_tag_document_chunks_impl(workflow_id, doc)
            # Audit per document actually tagged — anchored on the real
            # workflow_id + document hash (never an arbitrary/foreign batch id).
            db.log_audit(
                workflow_id=workflow_id,
                document_id=doc.get("document_id", ""),
                action_type="bulk_auto_tag",
                entity_type="document",
                metadata={
                    "actor": user.user_id,
                    "tagged_chunks": tagged.get("tagged_chunks", 0),
                    "total_tags": tagged.get("total_tags", 0),
                    "batch_size": len(workflow_ids),
                },
            )
            return BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=True,
                action=action,
                message=(
                    f"tagged_chunks={tagged.get('tagged_chunks', 0)};"
                    f"total_tags={tagged.get('total_tags', 0)}"
                ),
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            return BulkWorkflowActionResult(
                workflow_id=workflow_id, ok=False, action=action, message=detail
            )
        except Exception as exc:
            return BulkWorkflowActionResult(
                workflow_id=workflow_id, ok=False, action=action, message=str(exc)
            )

    if request.dry_run or len(workflow_ids) == 1:
        for workflow_id in workflow_ids:
            results.append(await _one(workflow_id))
    else:
        sem = asyncio.Semaphore(taxonomy.BULK_AUTO_TAG_CONCURRENCY)

        async def _gated(workflow_id: str) -> BulkWorkflowActionResult:
            async with sem:
                return await _one(workflow_id)

        results = list(await asyncio.gather(*[_gated(wid) for wid in workflow_ids]))

    succeeded = sum(1 for result in results if result.ok)
    failed = sum(1 for result in results if not result.ok)

    return BulkWorkflowActionResponse(
        action=action,
        dry_run=request.dry_run,
        requested=len(workflow_ids),
        succeeded=succeeded,
        failed=failed,
        results=results,
    )


@router.post("/documents/{workflow_id}/approve-ocr")
async def approve_ocr(workflow_id: str, user: RequireReview):
    """Approve OCR results and continue to chunking. Requires permission: review."""
    access.require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    await workflow_runtime.signal_workflow_approval(
        workflow_id, expected_stage="ocr_review", approval="ocr"
    )

    # Log approval
    document_service.log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="ocr_approved",
        new_value=True,
        metadata={"stage": "ocr_review", "next_stage": "translation_processing"}
    )

    return {"approved": "ocr", "workflow_id": workflow_id}


@router.post("/documents/{workflow_id}/approve-chunks")
async def approve_chunks(workflow_id: str, user: RequireReview):
    """Approve chunks and continue to prepare for ingestion."""
    access.require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    await workflow_runtime.signal_workflow_approval(
        workflow_id, expected_stage="chunk_review", approval="chunks"
    )

    # Log approval
    document_service.log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="chunks_approved",
        new_value=True,
        metadata={"stage": "chunk_review", "next_stage": "ready_for_ingestion"}
    )

    return {"approved": "chunks", "workflow_id": workflow_id}


@router.post("/documents/{workflow_id}/approve-translation")
async def approve_translation(workflow_id: str, user: RequireReview):
    """Approve translations and continue to chunking."""
    access.require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    await workflow_runtime.signal_workflow_approval(
        workflow_id, expected_stage="translation_review", approval="translation"
    )

    # Log approval
    document_service.log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="translation_approved",
        new_value=True,
        metadata={"stage": "translation_review", "next_stage": "chunking"}
    )

    return {"approved": "translation", "workflow_id": workflow_id}


@router.post("/documents/{workflow_id}/approve-ingestion")
async def approve_ingestion(workflow_id: str, user: RequireReview):
    """Approve ingestion and continue to Marqo ingestion."""
    access.require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    await workflow_runtime.signal_workflow_approval(
        workflow_id, expected_stage="ready_for_ingestion", approval="ingestion"
    )

    # Log approval
    document_service.log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="ingestion_approved",
        new_value=True,
        metadata={"stage": "ready_for_ingestion", "next_stage": "ingesting"}
    )

    return {"approved": "ingestion", "workflow_id": workflow_id}


@router.post("/documents/{workflow_id}/auto-tag-chunks")
async def auto_tag_document_chunks(workflow_id: str, user: RequireReview):
    """Re-run automatic domain tagging for all chunks in a document."""
    doc = access.require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    if doc.get("is_disabled"):
        raise HTTPException(400, "Cannot auto-tag a deleted document; restore it first")
    return await taxonomy.auto_tag_document_chunks_impl(workflow_id, doc)


@router.post("/documents/reconcile")
async def reconcile_document_states(user: RequirePipeline):
    """
    Reconcile SQLite document states with Temporal workflow states.

    This endpoint checks documents in processing/review stages and updates
    SQLite when Temporal reports a different live stage. It does **not** mark
    a document failed solely because the Temporal execution is gone or unqueryable
    (orphans with stale stages stay on their SQLite stage).

    Returns a summary of documents checked and updated.
    """
    # Stages that indicate an active workflow (not terminal states)
    active_stages = [
        'ocr_processing', 'ocr_review',
        'translation_processing', 'translation_review',
        'chunking', 'chunk_review',
        'ready_for_ingestion', 'ingesting'
    ]

    # Scope to caller's instances (None = data-unrestricted bypass / all tenants;
    # a control-plane master_admin has an empty scope → reconciles nothing).
    docs = db.list_documents(
        limit=1000,
        include_demo=True,
        include_disabled=True,
        instances=access.instance_scope_for_user(user),
    )
    active_docs = [d for d in docs if d.get('stage') in active_stages]

    results = {
        "checked": len(active_docs),
        "updated": 0,
        "still_running": 0,
        "skipped": 0,
        "details": []
    }

    for doc in active_docs:
        detail = await workflow_runtime.reconcile_single_document(doc)
        results["details"].append(detail)
        if detail.get("action") == "stage_synced" or detail.get("action") == "marked_failed":
            results["updated"] += 1
        elif detail.get("action") == "no_change":
            results["still_running"] += 1
        elif detail.get("action") in {"temporal_not_found", "temporal_unavailable"}:
            results["skipped"] += 1

    return results
