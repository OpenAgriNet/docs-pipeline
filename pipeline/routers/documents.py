"""Document CRUD, listing, metadata and per-document read views."""

import hashlib
import logging
from fastapi import APIRouter, File, HTTPException, Header, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from io import BytesIO
from pathlib import Path
from temporalio.client import WorkflowFailureError
from typing import Optional
from ..app import RATE_LIMIT_UPLOAD, limiter
from ..auth.deps import (
    CurrentUser,
    RequireAdmin,
    RequireReview,
    RequireSearch,
    RequireUpload,
)
from ..auth.permissions import Permission
from ..auth.tenancy import assert_document_instance_access, normalize_instance
from ..models import (
    AuditLogResponse,
    DocumentCohortsResponse,
    DocumentDetail,
    DocumentGraph,
    DocumentMetadataUpdate,
    DocumentQueryEnabledUpdate,
    DocumentStage,
    DocumentSummary,
    RegisterFolderRequest,
    RegisterRequest,
)
from ..workflows import DocumentPipelineWorkflow

router = APIRouter()


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
    create_instance = api._resolve_create_instance(user, instance)
    marqo_url = api._ignore_client_marqo_url(marqo_url)
    # Validate file path to prevent path traversal attacks
    filepath = api.validate_file_path(data.filepath)
    source_filename = api.get_filename_from_path(filepath)
    source_file_fingerprint = api._compute_file_fingerprint(filepath)
    canonical_document_id = source_file_fingerprint

    workflow_id = api._tenant_workflow_id(api.get_workflow_id(str(filepath)), create_instance)
    document_id = canonical_document_id

    # Reuse only when SQLite still tracks this workflow.
    # If SQLite was purged, avoid returning stale Temporal state and create a fresh run ID.
    existing_doc = api.db.get_document(workflow_id)
    if existing_doc:
        # Same fingerprint/path must not leak or restart another tenant's doc.
        existing_doc = assert_document_instance_access(user, existing_doc)
        try:
            handle = (await api.get_temporal_client()).get_workflow_handle(workflow_id)
            state = await handle.query("get_state")
            if state:
                return DocumentSummary(
                    document_id=document_id,
                    canonical_document_id=canonical_document_id,
                    workflow_id=workflow_id,
                    filename=source_filename,
                    source_filename=source_filename,
                    source_file_fingerprint=source_file_fingerprint,
                    authoritative=bool(existing_doc.get("source_manifest_name")) if existing_doc else False,
                    instance=normalize_instance(existing_doc.get("instance")),
                    stage=DocumentStage(state.get("stage", "registered")),
                    page_count=state.get("page_count", 0),
                    chunk_count=state.get("chunk_count", 0),
                    error_message=state.get("error_message"),
                )
        except HTTPException:
            raise
        except Exception:
            pass  # Workflow doesn't exist or is not queryable; proceed to new run
    else:
        workflow_id = api._rerun_workflow_id(workflow_id)

    # Start new workflow (tenant-tagged: memo + best-effort search attribute)
    handle = await api._start_pipeline_workflow(
        DocumentPipelineWorkflow.run,
        args=[
            document_id,
            api.get_filename_from_path(filepath),
            str(filepath),
            chunk_size,
            chunk_overlap,
            min_tokens,
            marqo_url,
            index_name,
            auto_approve,
            stop_after_ocr,
        ],
        id=workflow_id,
        instance=create_instance,
    )

    # Save to SQLite for visibility during processing
    api.db.upsert_document(
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
    job_id = api.db.create_document_job(
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
    api.db.update_document_fields(workflow_id, latest_job_id=job_id)

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
    Validates both file extension and PDF magic bytes for security.
    Rate limited to 10 requests/minute per IP.
    Client-supplied marqo_url is ignored; ingest resolves the endpoint from the environment.
    Requires permission: upload (no-op while AUTH_DISABLED=true).
    """
    create_instance = api._resolve_create_instance(user, instance)
    marqo_url = api._ignore_client_marqo_url(marqo_url)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in api.ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {suffix}")

    # Read file content
    content = await file.read()
    file_size = len(content)

    # Validate PDF magic bytes (%PDF-) only for PDF uploads
    if suffix == ".pdf":
        pdf_magic = b"%PDF-"
        if len(content) < 5 or content[:5] != pdf_magic:
            raise HTTPException(400, "Invalid PDF file: file does not have valid PDF header")

    # Generate unique object name, prefixed by tenant for storage isolation.
    file_hash = hashlib.md5(content).hexdigest()
    object_name = f"{create_instance}/{file_hash}/{file.filename}"

    # Upload to MinIO
    content_type = "application/pdf" if suffix == ".pdf" else "application/octet-stream"
    api.get_minio_client().put_object(
        api.MINIO_BUCKET,
        object_name,
        BytesIO(content),
        length=file_size,
        content_type=content_type
    )

    # Use minio:// URI as filepath
    minio_path = f"minio://{api.MINIO_BUCKET}/{object_name}"

    workflow_id = api._tenant_workflow_id(api.get_workflow_id(minio_path), create_instance)
    document_id = file_hash
    canonical_document_id = file_hash

    # Reuse only when SQLite still tracks this workflow.
    # If SQLite was purged, avoid returning stale Temporal state and create a fresh run ID.
    existing_doc = api.db.get_document(workflow_id)
    if existing_doc:
        existing_doc = assert_document_instance_access(user, existing_doc)
        try:
            handle = (await api.get_temporal_client()).get_workflow_handle(workflow_id)
            state = await handle.query("get_state")
            if state:
                return DocumentSummary(
                    document_id=document_id,
                    canonical_document_id=canonical_document_id,
                    workflow_id=workflow_id,
                    filename=file.filename,
                    source_filename=file.filename,
                    source_file_fingerprint=file_hash,
                    authoritative=bool(existing_doc.get("source_manifest_name")) if existing_doc else False,
                    instance=normalize_instance(existing_doc.get("instance")),
                    stage=DocumentStage(state.get("stage", "registered")),
                    page_count=state.get("page_count", 0),
                    chunk_count=state.get("chunk_count", 0),
                    error_message=state.get("error_message"),
                )
        except HTTPException:
            raise
        except Exception:
            pass
    else:
        workflow_id = api._rerun_workflow_id(workflow_id)

    # Start new workflow (tenant-tagged: memo + best-effort search attribute)
    handle = await api._start_pipeline_workflow(
        DocumentPipelineWorkflow.run,
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
        id=workflow_id,
        instance=create_instance,
    )

    # Save to SQLite for visibility during processing
    api.db.upsert_document(
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
    job_id = api.db.create_document_job(
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
    original_artifact_id = api.db.add_document_artifact(
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
    api.db.update_document_fields(
        workflow_id,
        latest_job_id=job_id,
        original_artifact_id=original_artifact_id,
        source_type=source_type,
        canonical_input_type=canonical_input_type,
        stop_after_ocr=1 if stop_after_ocr else 0,
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
    create_instance = api._resolve_create_instance(user, instance)
    directory = Path(data.directory)
    if not directory.exists():
        raise HTTPException(404, f"Directory not found: {data.directory}")

    candidate_files = [p for p in directory.glob("*") if p.is_file() and p.suffix.lower() in api.ALLOWED_EXTENSIONS]
    if not candidate_files:
        raise HTTPException(400, "No supported files found")

    results = []
    for pdf_path in candidate_files:
        try:
            result = await api.start_document_workflow(
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
                workflow_id=api.get_workflow_id(str(pdf_path)),
                filename=pdf_path.name,
                authoritative=False,
                stage=DocumentStage.FAILED,
                page_count=0,
                chunk_count=0,
                error_message="Failed to start workflow",
            ))

    return results


@router.get("/documents", response_model=list[DocumentSummary])
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
    """
    stage_filter = stage.value if stage else None
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"

    # Use SQLite only for fast listing - no Temporal queries
    docs = api.db.list_documents(
        stage=stage_filter,
        limit=limit,
        offset=offset,
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=api._instance_scope_for_user(user),
    )

    return [api._document_summary_from_row(doc) for doc in docs]


@router.get("/documents/summary", response_model=DocumentCohortsResponse)
async def get_documents_summary(
    user: CurrentUser,
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """Return aggregate SQLite counts for dashboard totals and migration planning."""
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    summary = api.db.get_document_summary_counts(
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=api._instance_scope_for_user(user),
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
    summary = api.db.get_document_summary_counts(
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=api._instance_scope_for_user(user),
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
    doc = api._require_document_for_user(workflow_id, user)
    return api._build_document_detail(doc)


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
    import traceback

    # Enforce tenant scope before touching Temporal (404 hides other tenants).
    api._require_document_for_user(workflow_id, user)

    try:
        handle = (await api.get_temporal_client()).get_workflow_handle(workflow_id)
        description = await handle.describe()
        
        result = {
            "workflow_id": workflow_id,
            "run_id": description.run_id,
            "status": description.status.name,
            "error_message": None,
            "error_type": None,
            "stack_trace": None,
            "has_error": False
        }
        
        # If workflow is failed, try to get detailed error information
        if description.status.name == "FAILED":
            result["has_error"] = True
            
            # Try to get error from workflow result (this raises WorkflowFailureError for failed workflows)
            try:
                await handle.result()
            except WorkflowFailureError as wf_err:
                # Extract error details from the failure
                result["error_message"] = str(wf_err)
                result["error_type"] = type(wf_err).__name__
                
                # Try to get the underlying cause
                if hasattr(wf_err, 'cause') and wf_err.cause:
                    cause = wf_err.cause
                    result["error_message"] = str(cause)
                    result["error_type"] = type(cause).__name__
                    
                    # Get stack trace if available
                    if hasattr(cause, '__traceback__') and cause.__traceback__:
                        result["stack_trace"] = ''.join(traceback.format_tb(cause.__traceback__))
                    elif hasattr(wf_err, '__traceback__') and wf_err.__traceback__:
                        result["stack_trace"] = ''.join(traceback.format_tb(wf_err.__traceback__))
                
                # Also try to get failure details from the exception itself
                if hasattr(wf_err, 'failure') and wf_err.failure:
                    failure = wf_err.failure
                    if hasattr(failure, 'message') and failure.message:
                        result["error_message"] = failure.message
                    if hasattr(failure, 'stack_trace') and failure.stack_trace:
                        result["stack_trace"] = failure.stack_trace
            except Exception as e:
                # If result() doesn't work, try other methods
                result["error_message"] = f"Could not retrieve error details: {str(e)}"
        
        # Also try to get error from workflow state query (fallback)
        if not result["error_message"]:
            try:
                state = await handle.query("get_state")
                if state and state.get("error_message"):
                    result["error_message"] = state.get("error_message")
                    result["has_error"] = True
            except Exception:
                pass  # Workflow might not support queries or be in wrong state
        
        return result
        
    except Exception as e:
        # If workflow doesn't exist or can't be accessed
        error_msg = str(e)
        if "not found" in error_msg.lower() or "workflow" in error_msg.lower():
            raise HTTPException(404, f"Workflow not found: {workflow_id}")
        raise HTTPException(500, f"Error fetching workflow details: {error_msg}")


@router.get("/documents/{workflow_id}/runtime")
async def get_document_runtime(workflow_id: str, user: RequireSearch):
    """Return live runtime status by combining SQLite state and Temporal workflow state."""
    doc = api._require_document_for_user(workflow_id, user)
    return await api._get_runtime_payload(workflow_id, doc=doc)


@router.get("/documents/{workflow_id}/artifacts")
async def list_document_artifacts(workflow_id: str, user: RequireSearch):
    api._require_document_for_user(workflow_id, user)
    return api.db.list_document_artifacts(workflow_id)


@router.get("/documents/{workflow_id}/artifacts/{artifact_id}")
async def get_document_artifact(workflow_id: str, user: RequireSearch, artifact_id: int):
    api._require_document_for_user(workflow_id, user)
    artifact = api.db.get_document_artifact(workflow_id, artifact_id)
    if not artifact:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")
    return artifact


@router.get("/documents/{workflow_id}/artifacts/{artifact_id}/content")
async def get_document_artifact_content(workflow_id: str, user: RequireSearch, artifact_id: int):
    api._require_document_for_user(workflow_id, user)
    artifact = api.db.get_document_artifact(workflow_id, artifact_id)
    if not artifact:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")

    storage_uri = artifact["storage_uri"]
    if storage_uri.startswith("minio://"):
        path = storage_uri.replace("minio://", "")
        bucket, object_name = path.split("/", 1)
        response = api.get_minio_client().get_object(bucket, object_name)
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
    api._require_document_for_user(workflow_id, user)
    return api.db.list_document_jobs(workflow_id, limit=limit)


@router.get("/documents/{workflow_id}/stage-io")
async def get_document_stage_io(workflow_id: str, user: RequireSearch):
    doc = api._require_document_for_user(workflow_id, user)
    return api._build_stage_io_payload(workflow_id, current_stage=doc.get("stage"))


@router.get("/documents/{workflow_id}/allowed-actions")
async def get_document_allowed_actions(workflow_id: str, user: RequireSearch):
    """Return the currently valid machine-facing actions for a document."""
    doc = api._require_document_for_user(workflow_id, user)
    return {
        "workflow_id": workflow_id,
        "stage": doc.get("stage"),
        "reindex_required": bool(doc.get("reindex_required")),
        "available_actions": api._list_available_actions(doc, api.db.get_latest_document_job(workflow_id)),
    }


@router.get("/documents/{workflow_id}/graph", response_model=DocumentGraph)
async def get_document_graph(workflow_id: str, user: RequireSearch):
    """Return a document-centric graph of state, jobs, artifacts, index status, and runtime."""
    doc = api._require_document_for_user(workflow_id, user)
    detail = api._build_document_detail(doc)
    return DocumentGraph(
        workflow_id=workflow_id,
        document=detail,
        jobs=api.db.list_document_jobs(workflow_id, limit=100),
        artifacts=detail.artifacts,
        index_status=detail.index_status,
        stage_io=api._build_stage_io_payload(workflow_id, current_stage=doc.get("stage")),
        runtime=await api._get_runtime_payload(workflow_id, doc=doc),
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
    doc = api._require_document_for_user(workflow_id, user, permission=Permission.ADMIN)

    result = {
        "workflow_id": workflow_id,
        "disabled": True,
        "workflow_cancelled": False,
        "chunks_excluded": 0,
        "marqo_deleted": 0
    }

    # Try to cancel workflow if still running
    try:
        handle = (await api.get_temporal_client()).get_workflow_handle(workflow_id)
        await handle.cancel()
        result["workflow_cancelled"] = True
    except Exception:
        pass  # Workflow already completed/cancelled

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
            target_index = api.resolve_index(doc.get("instance"), doc.get("index"))
            if target_index is not None:
                marqo_result = api.delete_chunks_from_marqo(
                    doc_id, index_name=target_index, workflow_id=workflow_id
                )
                result["marqo_deleted"] = int(marqo_result.get("deleted", 0) or 0)
                if marqo_result.get("error"):
                    raise HTTPException(502, f"Failed to remove document from Marqo: {marqo_result['error']}")

    # Mark as disabled in SQLite only after the purge succeeded.
    api.db.set_document_disabled(workflow_id, True)
    # Same semantics as unchecking Include: off for queries until reingest after restore.
    api.db.set_document_query_enabled(workflow_id, False)
    result["chunks_excluded"] = api.db.set_all_chunks_excluded(workflow_id, True)

    # Log audit
    api.db.log_audit(
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
    doc = api._require_document_for_user(workflow_id, user, permission=Permission.ADMIN)

    api.db.set_document_disabled(workflow_id, False)

    # Log audit
    api.db.log_audit(
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

    doc = api._require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    if doc.get("is_disabled"):
        raise HTTPException(400, "Cannot edit metadata on a deleted document; restore it first")

    old_name = doc.get("display_name")
    updated = api.db.set_document_display_name(workflow_id, body.display_name) or doc

    api.db.log_audit(
        workflow_id=workflow_id,
        document_id=updated.get("document_id", workflow_id),
        action_type="set_metadata",
        entity_type="document",
        field_name="display_name",
        old_value=old_name,
        new_value=updated.get("display_name"),
        metadata={"actor": user.user_id},
    )
    return api._document_summary_from_row(updated)


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
    doc = api._require_document_for_user(workflow_id, user, permission=Permission.ADMIN)
    was_enabled = bool(doc["query_enabled"]) if doc.get("query_enabled") is not None else True
    chunks_touched = 0
    marqo_deleted = 0

    if not body.query_enabled:
        # Purge Marqo before flipping DB so a failed purge does not leave
        # "queries off" while chunks remain searchable.
        doc_id = doc.get("document_id")
        if doc_id:
            target_index = api.resolve_index(doc.get("instance"), doc.get("index"))
            if target_index is not None:
                marqo_result = api.delete_chunks_from_marqo(
                    doc_id, index_name=target_index, workflow_id=workflow_id
                )
                marqo_deleted = int(marqo_result.get("deleted", 0) or 0)
                if marqo_result.get("error"):
                    raise HTTPException(502, f"Failed to remove document from Marqo: {marqo_result['error']}")
        chunks_touched = api.db.set_all_chunks_excluded(workflow_id, True)
        updated = api.db.set_document_query_enabled(workflow_id, False) or doc
    elif not was_enabled and body.query_enabled:
        updated = api.db.set_document_query_enabled(workflow_id, True) or doc
        chunks_touched = api.db.set_all_chunks_excluded(workflow_id, False)
        api._mark_reindex_required(
            workflow_id,
            "Document included for queries; reingest to republish chunks to Marqo",
            metadata={"actor": user.user_id},
        )
        updated = api.db.get_document(workflow_id) or updated
    else:
        updated = api.db.set_document_query_enabled(workflow_id, body.query_enabled) or doc

    api.db.log_audit(
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
    return api._document_summary_from_row(updated)


@router.post("/documents/{workflow_id}/demo")
async def set_document_demo(workflow_id: str, user: RequireAdmin, is_demo: bool = Query(True)):
    """
    Mark a document as demo.

    Demo documents are excluded from the UI by default but always available
    for API testing via include_demo=true parameter.
    """
    api._require_document_for_user(workflow_id, user, permission=Permission.ADMIN)
    api.db.set_document_demo(workflow_id, is_demo)
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
    api._require_document_for_user(workflow_id, user)
    logs = api.db.get_audit_logs(
        workflow_id=workflow_id,
        action_type=action_type,
        limit=limit,
        offset=offset
    )
    total = api.db.get_audit_log_count(workflow_id, action_type)

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
    doc = api._require_document_for_user(workflow_id, user)

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
            response = api.get_minio_client().get_object(bucket, object_name)

            return StreamingResponse(
                response,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": api._inline_content_disposition(filename)
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
                    "Content-Disposition": api._inline_content_disposition(filename)
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        # Log the actual error server-side but don't expose details to client
        logging.error(f"PDF serving error for {workflow_id}: {str(e)}")
        raise HTTPException(500, "Error serving PDF file")


# Imported last: `pipeline.api` re-exports the handlers above, so a top-level
# import here would be circular. Handlers resolve `api.<name>` at call time,
# which is what keeps `monkeypatch.setattr(api, ...)` biting.
from .. import api  # noqa: E402
