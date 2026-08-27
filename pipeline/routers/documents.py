"""Document CRUD, listing, metadata and per-document read views."""

import hashlib
import logging
from datetime import datetime
from fastapi import APIRouter, File, HTTPException, Header, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from io import BytesIO
from pathlib import Path
from pypdf import PdfReader
from typing import Optional
from .. import db
from ..auth.deps import (
    CurrentUser,
    RequireAdmin,
    RequireReview,
    RequireSearch,
    RequireUpload,
)
from ..auth.permissions import Permission
from ..models import (
    AuditLogResponse,
    DocumentCohortsResponse,
    DocumentDetail,
    DocumentGraph,
    DocumentListResponse,
    DocumentMetadataUpdate,
    DocumentQueryEnabledUpdate,
    DocumentStage,
    DocumentSummary,
    RegisterFolderRequest,
    RegisterRequest,
)
from ..rate_limit import RATE_LIMIT_UPLOAD, limiter
from ..services import access, documents as document_service, indexes, source_files, workflow_runtime
from ..storage import minio as minio_storage

router = APIRouter()


async def _start_document_pipeline_bound(*, workflow_id: str, instance: str, job_id: int, args: list):
    """Start DocumentPipelineWorkflow after the SQLite job row exists.

    If Temporal start fails, mark that job failed so a later retry cannot bind
    checkpoint state to a different run.
    """
    try:
        return await workflow_runtime.start_document_pipeline(
            args=[*args, job_id],
            id=workflow_id,
            instance=instance,
        )
    except Exception as exc:
        db.update_document_job(
            job_id,
            status="failed",
            completed_at=datetime.utcnow().isoformat(),
            error_message=str(exc),
        )
        db.update_document_fields(workflow_id, latest_job_id=job_id, error_message=str(exc))
        raise


@router.post("/documents", response_model=DocumentSummary)
@limiter.limit(RATE_LIMIT_UPLOAD)
async def start_document_workflow(
    request: Request,  # Required for rate limiting
    data: RegisterRequest,
    user: RequireUpload,
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    marqo_url: str = "",  # Ignored; the endpoint comes from the environment (SSRF)
    index_name: str = "documents-index",
    stop_after_ocr: bool = False,
    instance: str = "",
):
    """
    Start a new document processing workflow.

    The workflow will:
    1. Run OCR
    2. Wait for approval (unless auto_approve=True)
    3. Create chunks
    4. Wait for approval (unless auto_approve=True)
    5. Ingest to Marqo

    Note: File path must be within allowed directories (ALLOWED_FILE_PATHS env var).
    Rate limited to 10 requests/minute per IP.
    Client-supplied marqo_url is ignored; ingest resolves the endpoint from the environment.
    Requires permission: upload (no-op while AUTH_DISABLED=true).
    """
    create_instance = access.resolve_create_instance(user, instance)
    marqo_url = ""
    # Validate file path to prevent path traversal attacks
    filepath = source_files.validate_file_path(data.filepath)
    source_filename = source_files.get_filename_from_path(filepath)
    source_file_fingerprint = source_files.compute_file_fingerprint(filepath)
    canonical_document_id = source_file_fingerprint

    workflow_id = workflow_runtime.tenant_workflow_id(workflow_runtime.get_workflow_id(str(filepath)), create_instance)
    document_id = canonical_document_id

    deduped, workflow_id = await document_service.dedup_or_none(
        user,
        workflow_id,
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        filename=source_filename,
        source_filename=source_filename,
        source_file_fingerprint=source_file_fingerprint,
    )
    if deduped is not None:
        return deduped

    # Save to SQLite and bind the job BEFORE Temporal start so chunk checkpoints
    # attach to this run, not a prior job or a missing latest-job row.
    db.upsert_document(
        workflow_id=workflow_id,
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        filename=source_filename,
        source_filename=source_filename,
        source_file_fingerprint=source_file_fingerprint,
        filepath=str(filepath),
        stage="registered",
        stop_after_ocr=stop_after_ocr,
        instance=create_instance,
    )
    job_id = db.create_document_job(
        workflow_id=workflow_id,
        job_type="ocr_only" if stop_after_ocr else "pipeline",
        temporal_workflow_id=workflow_id,
        status="running",
        current_stage="registered",
        config={
            "auto_approve": auto_approve,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "min_tokens": min_tokens,
            "index_name": index_name,
            "stop_after_ocr": stop_after_ocr,
        },
    )
    db.update_document_fields(workflow_id, latest_job_id=job_id)
    await _start_document_pipeline_bound(
        workflow_id=workflow_id,
        instance=create_instance,
        job_id=job_id,
        args=[
            document_id,
            source_files.get_filename_from_path(filepath),
            str(filepath),
            chunk_size,
            chunk_overlap,
            min_tokens,
            marqo_url,
            index_name,
            auto_approve,
            stop_after_ocr,
        ],
    )

    return DocumentSummary(
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        workflow_id=workflow_id,
        filename=source_filename,
        source_filename=source_filename,
        source_file_fingerprint=source_file_fingerprint,
        authoritative=False,
        instance=create_instance,
        stage=DocumentStage.REGISTERED,
        page_count=0,
        chunk_count=0,
    )


@router.post("/upload", response_model=DocumentSummary)
@limiter.limit(RATE_LIMIT_UPLOAD)
async def upload_and_process(
    request: Request,  # Required for rate limiting
    user: RequireUpload,
    file: UploadFile = File(...),
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    marqo_url: str = "",
    index_name: str = "documents-index",
    stop_after_ocr: bool = False,
    instance: str = "",
):
    """
    Upload a supported file and start processing workflow.

    The file is stored in MinIO and then processed through the pipeline.
    Validates file extension; for PDFs also checks magic bytes and structural
    readability (via pypdf) before persistence.
    Rate limited to 10 requests/minute per IP.
    Client-supplied marqo_url is ignored; ingest resolves the endpoint from the environment.
    Requires permission: upload (no-op while AUTH_DISABLED=true).
    """
    create_instance = access.resolve_create_instance(user, instance)
    marqo_url = ""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in source_files.ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {suffix}")

    # Read file content
    content = await file.read()
    file_size = len(content)

    # Validate PDF magic bytes (%PDF-) and structural readability before MinIO.
    if suffix == ".pdf":
        pdf_magic = b"%PDF-"
        if len(content) < 5 or content[:5] != pdf_magic:
            raise HTTPException(400, "Invalid PDF file: file does not have valid PDF header")
        try:
            PdfReader(BytesIO(content))
        except Exception:
            raise HTTPException(
                400, "Invalid PDF file: file is not a structurally readable PDF"
            )

    # Generate unique object name, prefixed by tenant for storage isolation.
    file_hash = hashlib.md5(content).hexdigest()
    object_name = f"{create_instance}/{file_hash}/{file.filename}"

    # Upload to MinIO
    content_type = "application/pdf" if suffix == ".pdf" else "application/octet-stream"
    minio_storage.get_client().put_object(
        minio_storage.bucket_name(),
        object_name,
        BytesIO(content),
        length=file_size,
        content_type=content_type
    )

    # Use minio:// URI as filepath
    minio_path = f"minio://{minio_storage.bucket_name()}/{object_name}"

    workflow_id = workflow_runtime.tenant_workflow_id(workflow_runtime.get_workflow_id(minio_path), create_instance)
    document_id = file_hash
    canonical_document_id = file_hash

    deduped, workflow_id = await document_service.dedup_or_none(
        user,
        workflow_id,
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        filename=file.filename,
        source_filename=file.filename,
        source_file_fingerprint=file_hash,
    )
    if deduped is not None:
        return deduped

    # Save to SQLite and bind the job BEFORE Temporal start (same race as retry-chunking).
    db.upsert_document(
        workflow_id=workflow_id,
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        filename=file.filename,
        source_filename=file.filename,
        source_file_fingerprint=file_hash,
        filepath=minio_path,
        stage="registered",
        stop_after_ocr=stop_after_ocr,
        instance=create_instance,
    )
    job_id = db.create_document_job(
        workflow_id=workflow_id,
        job_type="ocr_only" if stop_after_ocr else "pipeline",
        temporal_workflow_id=workflow_id,
        status="running",
        current_stage="registered",
        config={
            "auto_approve": auto_approve,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "min_tokens": min_tokens,
            "index_name": index_name,
            "stop_after_ocr": stop_after_ocr,
        },
    )
    original_artifact_id = db.add_document_artifact(
        workflow_id=workflow_id,
        job_id=job_id,
        artifact_type="original_upload",
        stage="registered",
        storage_uri=minio_path,
        mime_type=content_type,
        filename=file.filename,
        size_bytes=file_size,
        metadata={"uploaded_via": "upload_endpoint"},
    )
    source_type = "spreadsheet" if suffix in {".csv", ".xlsx"} else "document"
    canonical_input_type = "spreadsheet" if suffix in {".csv", ".xlsx"} else "pdf"
    db.update_document_fields(
        workflow_id,
        latest_job_id=job_id,
        original_artifact_id=original_artifact_id,
        source_type=source_type,
        canonical_input_type=canonical_input_type,
        stop_after_ocr=1 if stop_after_ocr else 0,
    )
    await _start_document_pipeline_bound(
        workflow_id=workflow_id,
        instance=create_instance,
        job_id=job_id,
        args=[
            document_id,
            file.filename,
            minio_path,
            chunk_size,
            chunk_overlap,
            min_tokens,
            marqo_url,
            index_name,
            auto_approve,
            stop_after_ocr,
        ],
    )

    return DocumentSummary(
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        workflow_id=workflow_id,
        filename=file.filename,
        source_filename=file.filename,
        source_file_fingerprint=file_hash,
        authoritative=False,
        instance=create_instance,
        stage=DocumentStage.REGISTERED,
        page_count=0,
        chunk_count=0,
    )


@router.post("/documents/batch", response_model=list[DocumentSummary])
@limiter.limit("5/minute")  # Stricter limit for batch operations
async def start_batch_workflows(
    request: Request,  # Required for rate limiting
    data: RegisterFolderRequest,
    user: RequireUpload,
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    stop_after_ocr: bool = False,
    instance: str = "",
):
    """Start workflows for all supported documents in a directory."""
    create_instance = access.resolve_create_instance(user, instance)
    directory = Path(data.directory)
    if not directory.exists():
        raise HTTPException(404, f"Directory not found: {data.directory}")

    candidate_files = [p for p in directory.glob("*") if p.is_file() and p.suffix.lower() in source_files.ALLOWED_EXTENSIONS]
    if not candidate_files:
        raise HTTPException(400, "No supported files found")

    results = []
    for pdf_path in candidate_files:
        try:
            result = await start_document_workflow(
                request,
                RegisterRequest(filepath=str(pdf_path)),
                user,
                auto_approve=auto_approve,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                min_tokens=min_tokens,
                stop_after_ocr=stop_after_ocr,
                instance=create_instance,
            )
            results.append(result)
        except Exception as e:
            # Log full error, return sanitized message
            logging.error(f"Batch workflow error for {pdf_path.name}: {str(e)}")
            results.append(DocumentSummary(
                document_id=hashlib.md5(str(pdf_path).encode()).hexdigest(),
                workflow_id=workflow_runtime.get_workflow_id(str(pdf_path)),
                filename=pdf_path.name,
                authoritative=False,
                stage=DocumentStage.FAILED,
                page_count=0,
                chunk_count=0,
                error_message="Failed to start workflow",
            ))

    return results


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    user: CurrentUser,
    stage: Optional[DocumentStage] = None,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """
    List all document workflows.

    Uses SQLite for fast listing (no Temporal queries for performance).
    Demo documents are excluded by default - use X-Include-Demo: true header to show them.
    Disabled (soft-deleted) documents are excluded by default - use X-Include-Disabled: true to show them.
    Results are limited to instances the caller can access.

    Pagination:
    - limit: Max documents to return (default 100, max 500)
    - offset: Skip first N documents (default 0)
    - response includes total matching count under the same filters
    """
    stage_filter = stage.value if stage else None
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    instances = access.instance_scope_for_user(user)

    # Use SQLite only for fast listing - no Temporal queries
    docs = db.list_documents(
        stage=stage_filter,
        limit=limit,
        offset=offset,
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=instances,
    )
    total = db.count_documents(
        stage=stage_filter,
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=instances,
    )

    return DocumentListResponse(
        items=[document_service.document_summary_from_row(doc) for doc in docs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/documents/summary", response_model=DocumentCohortsResponse)
async def get_documents_summary(
    user: CurrentUser,
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """Return aggregate SQLite counts for dashboard totals and migration planning."""
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    summary = db.get_document_summary_counts(
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=access.instance_scope_for_user(user),
    )
    return {
        **summary,
        "by_stage": {
            "ocr_review": summary.get("ocr_review_documents", 0),
            "translation_review": summary.get("translation_review_documents", 0),
            "chunk_review": summary.get("chunk_review_documents", 0),
            "translation_processing": summary.get("translation_processing_documents", 0),
            "chunking": summary.get("chunking_documents", 0),
            "ready_for_ingestion": summary.get("ready_for_ingestion_documents", 0),
            "failed": summary.get("failed_documents", 0),
        },
    }


@router.get("/documents/cohorts", response_model=DocumentCohortsResponse)
async def get_document_cohorts(
    user: CurrentUser,
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """Return machine-friendly cohort counts for queueing and orchestration."""
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    summary = db.get_document_summary_counts(
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=access.instance_scope_for_user(user),
    )
    return {
        **summary,
        "by_stage": {
            "ocr_review": summary.get("ocr_review_documents", 0),
            "translation_review": summary.get("translation_review_documents", 0),
            "chunk_review": summary.get("chunk_review_documents", 0),
            "translation_processing": summary.get("translation_processing_documents", 0),
            "chunking": summary.get("chunking_documents", 0),
            "ready_for_ingestion": summary.get("ready_for_ingestion_documents", 0),
            "failed": summary.get("failed_documents", 0),
        },
    }


@router.get("/documents/{workflow_id}", response_model=DocumentDetail)
async def get_document(workflow_id: str, user: CurrentUser):
    """Get document workflow state with artifacts and indexing metadata."""
    doc = access.require_document_for_user(workflow_id, user)
    return document_service.build_document_detail(doc)


@router.get("/documents/{workflow_id}/error-details")
async def get_workflow_error_details(workflow_id: str, user: RequireSearch):
    """
    Get detailed error information from Temporal for a failed workflow.
    
    Returns comprehensive error details including:
    - Error message
    - Stack trace (if available)
    - Failure type
    - Workflow execution status
    
    This endpoint queries Temporal directly to get the most detailed
    error information available, which may be more complete than what's
    stored in SQLite.
    """
    # Enforce tenant scope before touching Temporal (404 hides other tenants).
    access.require_document_for_user(workflow_id, user)
    return await workflow_runtime.get_workflow_error_details(workflow_id)


@router.get("/documents/{workflow_id}/runtime")
async def get_document_runtime(workflow_id: str, user: RequireSearch):
    """Return live runtime status by combining SQLite state and Temporal workflow state."""
    doc = access.require_document_for_user(workflow_id, user)
    return await workflow_runtime.get_runtime_payload(workflow_id, doc=doc)


@router.get("/documents/{workflow_id}/artifacts")
async def list_document_artifacts(workflow_id: str, user: RequireSearch):
    access.require_document_for_user(workflow_id, user)
    return db.list_document_artifacts(workflow_id)


@router.get("/documents/{workflow_id}/artifacts/{artifact_id}")
async def get_document_artifact(workflow_id: str, user: RequireSearch, artifact_id: int):
    access.require_document_for_user(workflow_id, user)
    artifact = db.get_document_artifact(workflow_id, artifact_id)
    if not artifact:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")
    return artifact


@router.get("/documents/{workflow_id}/artifacts/{artifact_id}/content")
async def get_document_artifact_content(workflow_id: str, user: RequireSearch, artifact_id: int):
    access.require_document_for_user(workflow_id, user)
    artifact = db.get_document_artifact(workflow_id, artifact_id)
    if not artifact:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")

    storage_uri = artifact["storage_uri"]
    if storage_uri.startswith("minio://"):
        path = storage_uri.replace("minio://", "")
        bucket, object_name = path.split("/", 1)
        response = minio_storage.get_client().get_object(bucket, object_name)
        return StreamingResponse(
            response,
            media_type=artifact.get("mime_type") or "application/octet-stream",
            headers={"Content-Disposition": f'inline; filename="{artifact.get("filename") or "artifact"}"'},
        )

    file_path = Path(storage_uri)
    if not file_path.exists():
        raise HTTPException(404, "Artifact content not found")
    return StreamingResponse(
        open(file_path, "rb"),
        media_type=artifact.get("mime_type") or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{artifact.get("filename") or file_path.name}"'},
    )


@router.get("/documents/{workflow_id}/jobs")
async def list_document_jobs(workflow_id: str, user: RequireSearch, limit: int = Query(20, le=100)):
    access.require_document_for_user(workflow_id, user)
    return db.list_document_jobs(workflow_id, limit=limit)


@router.get("/documents/{workflow_id}/stage-io")
async def get_document_stage_io(workflow_id: str, user: RequireSearch):
    doc = access.require_document_for_user(workflow_id, user)
    return document_service.build_stage_io_payload(workflow_id, current_stage=doc.get("stage"))


@router.get("/documents/{workflow_id}/allowed-actions")
async def get_document_allowed_actions(workflow_id: str, user: RequireSearch):
    """Return the currently valid machine-facing actions for a document."""
    doc = access.require_document_for_user(workflow_id, user)
    return {
        "workflow_id": workflow_id,
        "stage": doc.get("stage"),
        "reindex_required": bool(doc.get("reindex_required")),
        "available_actions": document_service.list_available_actions(doc, db.get_latest_document_job(workflow_id)),
    }


@router.get("/documents/{workflow_id}/graph", response_model=DocumentGraph)
async def get_document_graph(workflow_id: str, user: RequireSearch):
    """Return a document-centric graph of state, jobs, artifacts, index status, and runtime."""
    doc = access.require_document_for_user(workflow_id, user)
    detail = document_service.build_document_detail(doc)
    return DocumentGraph(
        workflow_id=workflow_id,
        document=detail,
        jobs=db.list_document_jobs(workflow_id, limit=100),
        artifacts=detail.artifacts,
        index_status=detail.index_status,
        stage_io=document_service.build_stage_io_payload(workflow_id, current_stage=doc.get("stage")),
        runtime=await workflow_runtime.get_runtime_payload(workflow_id, doc=doc),
    )


@router.delete("/documents/{workflow_id}")
async def disable_document(
    workflow_id: str,
    user: RequireAdmin,
    remove_from_search: bool = Query(True),
):
    """
    Soft delete a document (disable it) with cascade to chunks.

    This performs a soft delete:
    - Marks the document as disabled in SQLite (hidden from list by default)
    - Turns query_enabled off and marks all SQLite chunks as excluded
    - Optionally removes all chunks from Marqo search index
    - Cancels the workflow if still running

    The document can be restored by calling POST /documents/{id}/restore.
    Use X-Include-Disabled: true header in list_documents to see disabled documents.

    Args:
        workflow_id: The document workflow ID
        remove_from_search: If True (default), removes chunks from Marqo index
    Requires permission: admin.
    """
    doc = access.require_document_for_user(workflow_id, user, permission=Permission.ADMIN)

    result = {
        "workflow_id": workflow_id,
        "disabled": True,
        "workflow_cancelled": False,
        "chunks_excluded": 0,
        "marqo_deleted": 0
    }

    # Try to cancel workflow if still running
    result["workflow_cancelled"] = await workflow_runtime.cancel_workflow_if_running(
        workflow_id
    )

    # Remove from Marqo FIRST if requested, so a failed purge cannot leave the
    # document marked disabled while its chunks stay searchable (mirror the
    # fail-closed ordering in set_document_query_enabled). Resolve the physical
    # index from the document's OWN tenant (never the hard-coded legacy
    # `documents-index`): a per-tenant delete must target that tenant's index, and
    # must NEVER delete the DEFAULT tenant's records out of the legacy index via a
    # content-md5 doc_id collision. When the tenant has no index of its own,
    # nothing is indexed for it — skip the Marqo deletion entirely.
    if remove_from_search:
        doc_id = doc.get("document_id")
        if doc_id:
            target_index = indexes.resolve_index(doc.get("instance"), doc.get("index"))
            if target_index is not None:
                marqo_result = indexes.delete_chunks_from_marqo(
                    doc_id, index_name=target_index, workflow_id=workflow_id
                )
                result["marqo_deleted"] = int(marqo_result.get("deleted", 0) or 0)
                if marqo_result.get("error"):
                    raise HTTPException(502, f"Failed to remove document from Marqo: {marqo_result['error']}")

    # Mark as disabled in SQLite only after the purge succeeded.
    db.set_document_disabled(workflow_id, True)
    # Same semantics as unchecking Include: off for queries until reingest after restore.
    db.set_document_query_enabled(workflow_id, False)
    result["chunks_excluded"] = db.set_all_chunks_excluded(workflow_id, True)

    # Log audit
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", ""),
        action_type="disable_document",
        metadata={
            "remove_from_search": remove_from_search,
            "chunks_excluded": result["chunks_excluded"],
            "marqo_deleted": result["marqo_deleted"],
            "query_enabled": False,
        },
    )

    return result


@router.post("/documents/{workflow_id}/restore")
async def restore_document(workflow_id: str, user: RequireAdmin):
    """
    Restore a soft-deleted (disabled) document into the list.

    Chunks stay excluded and out of Marqo until the operator enables the
    document for queries and reingests.
    """
    doc = access.require_document_for_user(workflow_id, user, permission=Permission.ADMIN)

    db.set_document_disabled(workflow_id, False)

    # Log audit
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", ""),
        action_type="restore_document",
        metadata={"note": "chunks remain excluded; reingest required to republish"},
    )

    return {
        "workflow_id": workflow_id,
        "restored": True,
        "query_enabled": bool(doc["query_enabled"]) if doc.get("query_enabled") is not None else False,
    }


@router.patch("/documents/{workflow_id}/metadata", response_model=DocumentSummary)
async def update_document_metadata(
    workflow_id: str,
    body: DocumentMetadataUpdate,
    user: RequireReview,
):
    """Update human-facing document metadata (display name).

    Does not change tenant, fingerprint, document_id, or pipeline stage.
    Empty ``display_name`` clears the override so the UI falls back to filename.
    """
    if body.display_name is None:
        raise HTTPException(400, "Provide display_name (use empty string to clear)")

    doc = access.require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    if doc.get("is_disabled"):
        raise HTTPException(400, "Cannot edit metadata on a deleted document; restore it first")

    old_name = doc.get("display_name")
    updated = db.set_document_display_name(workflow_id, body.display_name) or doc

    db.log_audit(
        workflow_id=workflow_id,
        document_id=updated.get("document_id", workflow_id),
        action_type="set_metadata",
        entity_type="document",
        field_name="display_name",
        old_value=old_name,
        new_value=updated.get("display_name"),
        metadata={"actor": user.user_id},
    )
    return document_service.document_summary_from_row(updated)


@router.post("/documents/{workflow_id}/query-enabled", response_model=DocumentSummary)
async def set_document_query_enabled(
    workflow_id: str,
    body: DocumentQueryEnabledUpdate,
    user: RequireAdmin,
):
    """Enable or disable a document for search queries.

    When disabled: all chunks are excluded (same as unchecking Include on each)
    and fully removed from Marqo. When enabled: chunks are included again and
    reindex is marked required (reingest republishes to Marqo).
    This does not soft-delete the document (it stays in the list).
    """
    doc = access.require_document_for_user(workflow_id, user, permission=Permission.ADMIN)
    was_enabled = bool(doc["query_enabled"]) if doc.get("query_enabled") is not None else True
    chunks_touched = 0
    marqo_deleted = 0

    if not body.query_enabled:
        # Purge Marqo before flipping DB so a failed purge does not leave
        # "queries off" while chunks remain searchable.
        doc_id = doc.get("document_id")
        if doc_id:
            target_index = indexes.resolve_index(doc.get("instance"), doc.get("index"))
            if target_index is not None:
                marqo_result = indexes.delete_chunks_from_marqo(
                    doc_id, index_name=target_index, workflow_id=workflow_id
                )
                marqo_deleted = int(marqo_result.get("deleted", 0) or 0)
                if marqo_result.get("error"):
                    raise HTTPException(502, f"Failed to remove document from Marqo: {marqo_result['error']}")
        chunks_touched = db.set_all_chunks_excluded(workflow_id, True)
        updated = db.set_document_query_enabled(workflow_id, False) or doc
    elif not was_enabled and body.query_enabled:
        updated = db.set_document_query_enabled(workflow_id, True) or doc
        chunks_touched = db.set_all_chunks_excluded(workflow_id, False)
        document_service.mark_reindex_required(
            workflow_id,
            "Document included for queries; reingest to republish chunks to Marqo",
            metadata={"actor": user.user_id},
        )
        updated = db.get_document(workflow_id) or updated
    else:
        updated = db.set_document_query_enabled(workflow_id, body.query_enabled) or doc

    db.log_audit(
        workflow_id=workflow_id,
        document_id=updated.get("document_id", workflow_id),
        action_type="set_query_enabled",
        field_name="query_enabled",
        old_value=str(was_enabled).lower(),
        new_value=str(bool(body.query_enabled)).lower(),
        metadata={
            "actor": user.user_id,
            "chunks_touched": chunks_touched,
            "marqo_deleted": marqo_deleted,
        },
    )
    return document_service.document_summary_from_row(updated)


@router.post("/documents/{workflow_id}/demo")
async def set_document_demo(workflow_id: str, user: RequireAdmin, is_demo: bool = Query(True)):
    """
    Mark a document as demo.

    Demo documents are excluded from the UI by default but always available
    for API testing via include_demo=true parameter.
    """
    access.require_document_for_user(workflow_id, user, permission=Permission.ADMIN)
    db.set_document_demo(workflow_id, is_demo)
    return {"workflow_id": workflow_id, "is_demo": is_demo}


@router.get("/documents/{workflow_id}/audit", response_model=AuditLogResponse)
async def get_document_audit_log(
    workflow_id: str,
    user: RequireSearch,
    action_type: str = None,
    limit: int = Query(50, le=200),
    offset: int = 0
):
    """
    Get audit trail for a document.

    Returns a list of all changes made to the document including:
    - Stage transitions
    - Page edits
    - Chunk edits
    - Approvals
    - Resets
    """
    access.require_document_for_user(workflow_id, user)
    logs = db.get_audit_logs(
        workflow_id=workflow_id,
        action_type=action_type,
        limit=limit,
        offset=offset
    )
    total = db.get_audit_log_count(workflow_id, action_type)

    return AuditLogResponse(
        logs=logs,
        total=total,
        limit=limit,
        offset=offset
    )


@router.get("/documents/{workflow_id}/pdf")
async def get_document_pdf(workflow_id: str, user: RequireSearch):
    """
    Get the original PDF file for a document.
    Returns the PDF as a streaming response. SQLite-first for speed.
    """
    # SQLite-first - instant lookup, tenant-scoped (404 hides other tenants).
    doc = access.require_document_for_user(workflow_id, user)

    filepath = doc.get("filepath", "")
    filename = doc.get("filename", "document.pdf")

    if not filepath:
        raise HTTPException(404, f"Document has no PDF path: {workflow_id}")

    try:
        if filepath.startswith("minio://"):
            # Parse minio://bucket/object/path
            path = filepath.replace("minio://", "")
            parts = path.split("/", 1)
            bucket = parts[0]
            object_name = parts[1] if len(parts) > 1 else ""

            # Get object from MinIO
            response = minio_storage.get_client().get_object(bucket, object_name)

            return StreamingResponse(
                response,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": document_service.inline_content_disposition(filename)
                }
            )
        else:
            # Local file
            file_path = Path(filepath)
            if not file_path.exists():
                raise HTTPException(404, f"PDF file not found: {filepath}")

            def file_iterator():
                with open(file_path, "rb") as f:
                    yield from f

            return StreamingResponse(
                file_iterator(),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": document_service.inline_content_disposition(filename)
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        # Log the actual error server-side but don't expose details to client
        logging.error(f"PDF serving error for {workflow_id}: {str(e)}")
        raise HTTPException(500, "Error serving PDF file")
