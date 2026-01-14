"""
FastAPI REST API for the Temporal-based OCR pipeline.

This API provides HTTP endpoints that interact with Temporal workflows.
"""

import os
import json
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager
from io import BytesIO

from fastapi import FastAPI, HTTPException, Query, Path as PathParam, UploadFile, File, Header, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from temporalio.client import Client
from minio import Minio
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from .models import (
    RegisterRequest, RegisterFolderRequest, PageUpdate, ChunkUpdate,
    ApprovalRequest, DocumentSummary, DocumentStage, PIPELINE_STAGES,
    AuditLogResponse, SearchSettings, SearchSettingsUpdate, SettingsAuditResponse
)
from .workflows import DocumentPipelineWorkflow, ReingestionWorkflow
from . import db

TASK_QUEUE = "ocr-pipeline"

# Global clients
temporal_client: Optional[Client] = None
minio_client: Optional[Minio] = None
MINIO_BUCKET = "documents"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Temporal and MinIO clients on startup."""
    global temporal_client, minio_client, MINIO_BUCKET

    # Initialize SQLite database
    print("Initializing SQLite database...")
    db.init_db()

    # Temporal
    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    print(f"Connecting to Temporal at {temporal_host}")
    temporal_client = await Client.connect(temporal_host)

    # MinIO - credentials required via environment variables
    minio_endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    minio_access_key = os.environ.get("MINIO_ACCESS_KEY")
    minio_secret_key = os.environ.get("MINIO_SECRET_KEY")
    MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "documents")

    if not minio_access_key or not minio_secret_key:
        raise RuntimeError("MINIO_ACCESS_KEY and MINIO_SECRET_KEY environment variables are required")

    print(f"Connecting to MinIO at {minio_endpoint}")
    minio_client = Minio(
        minio_endpoint,
        access_key=minio_access_key,
        secret_key=minio_secret_key,
        secure=False
    )

    # Ensure bucket exists
    if not minio_client.bucket_exists(MINIO_BUCKET):
        minio_client.make_bucket(MINIO_BUCKET)
        print(f"Created MinIO bucket: {MINIO_BUCKET}")

    yield
    # Cleanup if needed


app = FastAPI(
    title="Document Ingestion Pipeline API",
    description="""
REST API for the Temporal-based OCR pipeline with translation support.

## Workflow Stages

1. `registered` - Document registered
2. `ocr_processing` - OCR in progress
3. `ocr_review` - **Waiting for OCR review/approval**
4. `translation_processing` - Translating non-English content
5. `translation_review` - **Waiting for translation review/approval**
6. `chunking` - Chunking in progress
7. `chunk_review` - **Waiting for chunk review/approval**
8. `ready_for_ingestion` - **Waiting for final approval**
9. `ingesting` - Ingesting to Marqo
10. `completed` - Done
11. `failed` - Error occurred

## Review Flow

1. Start workflow with `POST /upload` or `POST /documents`
2. Wait for `ocr_review` stage
3. Review/edit pages with `GET/PATCH /documents/{id}/pages/{num}`
4. Approve with `POST /documents/{id}/approve-ocr`
5. Wait for `translation_review` stage
6. Review/edit translations with `PATCH /documents/{id}/pages/{num}`
7. Approve with `POST /documents/{id}/approve-translation`
8. Wait for `chunk_review` stage
9. Review/edit chunks with `GET/PATCH /documents/{id}/chunks/{num}`
10. Approve with `POST /documents/{id}/approve-chunks`
11. Wait for `ready_for_ingestion` stage
12. Final approval with `POST /documents/{id}/approve-ingestion`
13. Workflow completes automatically
    """,
    version="2.0.0",
    lifespan=lifespan
)

# CORS configuration - explicit origins for security
ALLOWED_ORIGINS = os.environ.get("CORS_ORIGINS", "https://localhost:3000,http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Rate limiting configuration
# Default: 100 requests/minute for general endpoints, 10/minute for uploads
RATE_LIMIT_DEFAULT = os.environ.get("RATE_LIMIT_DEFAULT", "100/minute")
RATE_LIMIT_UPLOAD = os.environ.get("RATE_LIMIT_UPLOAD", "10/minute")

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Allowed base directories for file access (configurable via env)
ALLOWED_FILE_PATHS = os.environ.get("ALLOWED_FILE_PATHS", "/app/books,/data/documents").split(",")


def validate_file_path(filepath: str) -> Path:
    """
    Validate that a file path is within allowed directories.
    Prevents path traversal attacks.

    Raises HTTPException if path is not allowed.
    """
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
            if not path.suffix.lower() == '.pdf':
                raise HTTPException(400, "Only PDF files are allowed")
            return path
        except ValueError:
            continue  # Not within this allowed path, try next

    # Path not within any allowed directory
    raise HTTPException(403, "Access to this file path is not allowed")


def get_workflow_id(filepath: str) -> str:
    """Generate consistent workflow ID from filepath."""
    return f"doc-{hashlib.md5(filepath.encode()).hexdigest()[:12]}"


def delete_single_chunk_from_marqo(doc_id: str, chunk_num: int, index_name: str = "documents-index") -> dict:
    """
    Delete a single chunk from Marqo by doc_id and chunk_num.

    Args:
        doc_id: The document_id hash used in Marqo's doc_id field
        chunk_num: The chunk number to delete
        index_name: Marqo index name

    Returns:
        Dict with deletion result
    """
    import marqo

    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    mq = marqo.Client(url=marqo_url)

    try:
        index = mq.index(index_name)

        # Search for the specific chunk
        results = index.search(
            q="",
            filter_string=f"doc_id:{doc_id} AND chunk_num:{chunk_num}",
            limit=1,
            attributes_to_retrieve=["_id"]
        )

        if not results.get("hits"):
            return {"deleted": False, "reason": "not_found"}

        # Delete the chunk
        chunk_id = results["hits"][0]["_id"]
        index.delete_documents(ids=[chunk_id])

        return {"deleted": True, "chunk_id": chunk_id}

    except Exception as e:
        return {"deleted": False, "error": str(e)}


def delete_chunks_from_marqo(doc_id: str, index_name: str = "documents-index") -> dict:
    """
    Delete all chunks for a document from Marqo.

    Args:
        doc_id: The document_id hash used in Marqo's doc_id field
        index_name: Marqo index name

    Returns:
        Dict with deletion stats
    """
    import marqo

    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    mq = marqo.Client(url=marqo_url)

    try:
        index = mq.index(index_name)

        # Search for all documents with this doc_id
        # Marqo doesn't have delete by filter, so we need to find IDs first
        results = index.search(
            q="",
            filter_string=f"doc_id:{doc_id}",
            limit=1000,  # Get all chunks for this document
            attributes_to_retrieve=["_id"]
        )

        if not results.get("hits"):
            return {"deleted": 0, "doc_id": doc_id}

        # Extract IDs and delete
        ids_to_delete = [hit["_id"] for hit in results["hits"]]
        if ids_to_delete:
            index.delete_documents(ids=ids_to_delete)

        return {"deleted": len(ids_to_delete), "doc_id": doc_id}

    except Exception as e:
        # Index might not exist or other error
        return {"deleted": 0, "doc_id": doc_id, "error": str(e)}


# =============================================================================
# Document Routes
# =============================================================================

@app.post("/documents", response_model=DocumentSummary)
@limiter.limit(RATE_LIMIT_UPLOAD)
async def start_document_workflow(
    request: Request,  # Required for rate limiting
    data: RegisterRequest,
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    marqo_url: str = "",  # Empty = use MARQO_URL env var
    index_name: str = "documents-index"
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
    """
    # Validate file path to prevent path traversal attacks
    filepath = validate_file_path(data.filepath)

    workflow_id = get_workflow_id(str(filepath))
    document_id = hashlib.md5(str(filepath).encode()).hexdigest()

    # Check if workflow already exists
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query(DocumentPipelineWorkflow.get_state)
        if state:
            return DocumentSummary(
                document_id=document_id,
                workflow_id=workflow_id,
                filename=filepath.name,
                stage=DocumentStage(state.get("stage", "registered")),
                page_count=state.get("page_count", 0),
                chunk_count=state.get("chunk_count", 0),
                error_message=state.get("error_message")
            )
    except Exception:
        pass  # Workflow doesn't exist, create new

    # Start new workflow
    handle = await temporal_client.start_workflow(
        DocumentPipelineWorkflow.run,
        args=[
            document_id,
            filepath.name,
            str(filepath),
            chunk_size,
            chunk_overlap,
            min_tokens,
            marqo_url,
            index_name,
            auto_approve
        ],
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )

    # Save to SQLite for visibility during processing
    db.upsert_document(
        workflow_id=workflow_id,
        document_id=document_id,
        filename=filepath.name,
        filepath=str(filepath),
        stage="registered"
    )

    return DocumentSummary(
        document_id=document_id,
        workflow_id=workflow_id,
        filename=filepath.name,
        stage=DocumentStage.REGISTERED,
        page_count=0,
        chunk_count=0
    )


@app.post("/upload", response_model=DocumentSummary)
@limiter.limit(RATE_LIMIT_UPLOAD)
async def upload_and_process(
    request: Request,  # Required for rate limiting
    file: UploadFile = File(...),
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    marqo_url: str = "",
    index_name: str = "documents-index"
):
    """
    Upload a PDF file and start processing workflow.

    The file is stored in MinIO and then processed through the pipeline.
    Validates both file extension and PDF magic bytes for security.
    Rate limited to 10 requests/minute per IP.
    """
    # Check file extension
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(400, "Only PDF files are allowed")

    # Read file content
    content = await file.read()
    file_size = len(content)

    # Validate PDF magic bytes (%PDF-)
    PDF_MAGIC = b'%PDF-'
    if len(content) < 5 or content[:5] != PDF_MAGIC:
        raise HTTPException(400, "Invalid PDF file: file does not have valid PDF header")

    # Generate unique object name
    file_hash = hashlib.md5(content).hexdigest()
    object_name = f"{file_hash}/{file.filename}"

    # Upload to MinIO
    minio_client.put_object(
        MINIO_BUCKET,
        object_name,
        BytesIO(content),
        length=file_size,
        content_type="application/pdf"
    )

    # Use minio:// URI as filepath
    minio_path = f"minio://{MINIO_BUCKET}/{object_name}"

    workflow_id = get_workflow_id(minio_path)
    document_id = file_hash

    # Check if workflow already exists
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query(DocumentPipelineWorkflow.get_state)
        if state:
            return DocumentSummary(
                document_id=document_id,
                workflow_id=workflow_id,
                filename=file.filename,
                stage=DocumentStage(state.get("stage", "registered")),
                page_count=state.get("page_count", 0),
                chunk_count=state.get("chunk_count", 0),
                error_message=state.get("error_message")
            )
    except Exception:
        pass

    # Start new workflow
    handle = await temporal_client.start_workflow(
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
            auto_approve
        ],
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )

    # Save to SQLite for visibility during processing
    db.upsert_document(
        workflow_id=workflow_id,
        document_id=document_id,
        filename=file.filename,
        filepath=minio_path,
        stage="registered"
    )

    return DocumentSummary(
        document_id=document_id,
        workflow_id=workflow_id,
        filename=file.filename,
        stage=DocumentStage.REGISTERED,
        page_count=0,
        chunk_count=0
    )


@app.post("/documents/batch", response_model=list[DocumentSummary])
@limiter.limit("5/minute")  # Stricter limit for batch operations
async def start_batch_workflows(
    request: Request,  # Required for rate limiting
    data: RegisterFolderRequest,
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
):
    """Start workflows for all PDFs in a directory."""
    directory = Path(data.directory)
    if not directory.exists():
        raise HTTPException(404, f"Directory not found: {data.directory}")

    pdf_files = list(directory.glob("*.pdf"))
    if not pdf_files:
        raise HTTPException(400, "No PDF files found")

    results = []
    for pdf_path in pdf_files:
        try:
            result = await start_document_workflow(
                RegisterRequest(filepath=str(pdf_path)),
                auto_approve=auto_approve,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                min_tokens=min_tokens,
            )
            results.append(result)
        except Exception as e:
            # Log full error, return sanitized message
            logging.error(f"Batch workflow error for {pdf_path.name}: {str(e)}")
            results.append(DocumentSummary(
                document_id=hashlib.md5(str(pdf_path).encode()).hexdigest(),
                workflow_id=get_workflow_id(str(pdf_path)),
                filename=pdf_path.name,
                stage=DocumentStage.FAILED,
                page_count=0,
                chunk_count=0,
                error_message="Failed to start workflow"
            ))

    return results


@app.get("/documents", response_model=list[DocumentSummary])
async def list_documents(
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

    Pagination:
    - limit: Max documents to return (default 100, max 500)
    - offset: Skip first N documents (default 0)
    """
    stage_filter = stage.value if stage else None
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"

    # Use SQLite only for fast listing - no Temporal queries
    docs = db.list_documents(
        stage=stage_filter,
        limit=limit,
        offset=offset,
        include_demo=include_demo,
        include_disabled=include_disabled
    )

    return [
        DocumentSummary(
            document_id=doc["document_id"],
            workflow_id=doc["workflow_id"],
            filename=doc["filename"],
            stage=DocumentStage(doc["stage"]),
            page_count=doc["page_count"],
            chunk_count=doc["chunk_count"],
            error_message=doc["error_message"]
        )
        for doc in docs
    ]


@app.get("/documents/{workflow_id}")
async def get_document(workflow_id: str):
    """Get document workflow state."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query(DocumentPipelineWorkflow.get_state)
        return state
    except Exception as e:
        # Fallback to SQLite for completed/failed workflows
        doc = db.get_document(workflow_id)
        if doc:
            return doc
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.delete("/documents/{workflow_id}")
async def disable_document(workflow_id: str, remove_from_search: bool = Query(True)):
    """
    Soft delete a document (disable it).

    This performs a soft delete:
    - Marks the document as disabled in SQLite (hidden from list by default)
    - Optionally removes all chunks from Marqo search index
    - Cancels the workflow if still running

    The document can be restored by calling POST /documents/{id}/restore.
    Use X-Include-Disabled: true header in list_documents to see disabled documents.

    Args:
        workflow_id: The document workflow ID
        remove_from_search: If True (default), removes chunks from Marqo index
    """
    # Get document to ensure it exists
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")

    result = {
        "workflow_id": workflow_id,
        "disabled": True,
        "workflow_cancelled": False,
        "marqo_deleted": 0
    }

    # Try to cancel workflow if still running
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.cancel()
        result["workflow_cancelled"] = True
    except Exception:
        pass  # Workflow already completed/cancelled

    # Mark as disabled in SQLite
    db.set_document_disabled(workflow_id, True)

    # Remove from Marqo if requested
    if remove_from_search:
        doc_id = doc.get("document_id")
        if doc_id:
            marqo_result = delete_chunks_from_marqo(doc_id)
            result["marqo_deleted"] = marqo_result.get("deleted", 0)
            if "error" in marqo_result:
                result["marqo_error"] = marqo_result["error"]

    # Log audit
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", ""),
        action_type="disable_document",
        metadata={"remove_from_search": remove_from_search, "marqo_deleted": result["marqo_deleted"]}
    )

    return result


@app.post("/documents/{workflow_id}/restore")
async def restore_document(workflow_id: str):
    """
    Restore a soft-deleted (disabled) document.

    Note: This only restores the document in SQLite. Chunks that were removed
    from Marqo will NOT be automatically re-indexed. To re-index, you would
    need to re-run the ingestion process.
    """
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")

    db.set_document_disabled(workflow_id, False)

    # Log audit
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", ""),
        action_type="restore_document"
    )

    return {"workflow_id": workflow_id, "restored": True}


@app.post("/documents/{workflow_id}/reingest")
async def reingest_document(
    workflow_id: str,
    marqo_url: str = "",
    index_name: str = "documents-index"
):
    """
    Re-ingest a completed document to Marqo.

    Use this to re-ingest documents that completed but weren't properly
    indexed (e.g., due to index schema changes). This starts a lightweight
    workflow that uses chunks already stored in SQLite.

    The document must have chunks stored in SQLite (typically from a
    completed or previously ingested document).
    """
    # Get document from SQLite
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")

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

    # Start re-ingestion workflow
    await temporal_client.start_workflow(
        ReingestionWorkflow.run,
        args=[
            document_id,
            filename,
            workflow_id,  # original workflow_id for SQLite updates
            chunks,
            page_count,
            marqo_url,
            index_name
        ],
        id=reingest_workflow_id,
        task_queue=TASK_QUEUE,
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


@app.post("/documents/{workflow_id}/demo")
async def set_document_demo(workflow_id: str, is_demo: bool = Query(True)):
    """
    Mark a document as demo.

    Demo documents are excluded from the UI by default but always available
    for API testing via include_demo=true parameter.
    """
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")

    db.set_document_demo(workflow_id, is_demo)
    return {"workflow_id": workflow_id, "is_demo": is_demo}


# =============================================================================
# Approval Routes
# =============================================================================

async def _validate_approval_stage(workflow_id: str, expected_stage: str):
    """Validate that workflow is in the expected stage before approval."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query(DocumentPipelineWorkflow.get_state)
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


@app.post("/documents/{workflow_id}/approve-ocr")
async def approve_ocr(workflow_id: str):
    """Approve OCR results and continue to chunking."""
    handle = await _validate_approval_stage(workflow_id, "ocr_review")
    await handle.signal(DocumentPipelineWorkflow.approve_ocr)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="ocr_approved",
        new_value=True,
        metadata={"stage": "ocr_review", "next_stage": "translation_processing"}
    )

    return {"approved": "ocr", "workflow_id": workflow_id}


@app.post("/documents/{workflow_id}/approve-chunks")
async def approve_chunks(workflow_id: str):
    """Approve chunks and continue to prepare for ingestion."""
    handle = await _validate_approval_stage(workflow_id, "chunk_review")
    await handle.signal(DocumentPipelineWorkflow.approve_chunks)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="chunks_approved",
        new_value=True,
        metadata={"stage": "chunk_review", "next_stage": "ready_for_ingestion"}
    )

    return {"approved": "chunks", "workflow_id": workflow_id}


@app.post("/documents/{workflow_id}/approve-translation")
async def approve_translation(workflow_id: str):
    """Approve translations and continue to chunking."""
    handle = await _validate_approval_stage(workflow_id, "translation_review")
    await handle.signal(DocumentPipelineWorkflow.approve_translation)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="translation_approved",
        new_value=True,
        metadata={"stage": "translation_review", "next_stage": "chunking"}
    )

    return {"approved": "translation", "workflow_id": workflow_id}


@app.post("/documents/{workflow_id}/approve-ingestion")
async def approve_ingestion(workflow_id: str):
    """Approve ingestion and continue to Marqo ingestion."""
    handle = await _validate_approval_stage(workflow_id, "ready_for_ingestion")
    await handle.signal(DocumentPipelineWorkflow.approve_ingestion)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="ingestion_approved",
        new_value=True,
        metadata={"stage": "ready_for_ingestion", "next_stage": "ingesting"}
    )

    return {"approved": "ingestion", "workflow_id": workflow_id}


# =============================================================================
# Audit Log Helper and Routes
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


@app.get("/audit", response_model=AuditLogResponse)
async def get_all_audit_logs(
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
    """
    logs = db.get_all_audit_logs(
        action_type=action_type,
        limit=limit,
        offset=offset
    )
    total = db.get_all_audit_log_count(action_type)

    return AuditLogResponse(
        logs=logs,
        total=total,
        limit=limit,
        offset=offset
    )


@app.get("/documents/{workflow_id}/audit", response_model=AuditLogResponse)
async def get_document_audit_log(
    workflow_id: str,
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


# =============================================================================
# Page Routes (OCR Review)
# =============================================================================

@app.get("/documents/{workflow_id}/pages")
async def list_pages(workflow_id: str):
    """Get all pages for a document."""
    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        pages = await handle.query(DocumentPipelineWorkflow.get_pages)
        return pages
    except Exception:
        pass  # Fall back to SQLite

    # Fall back to SQLite for completed/unavailable workflows
    pages = db.get_pages(workflow_id)
    if pages:
        return pages

    raise HTTPException(404, f"Document not found: {workflow_id}")


@app.get("/documents/{workflow_id}/pages/{page_num}")
async def get_page(workflow_id: str, page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)")):
    """Get a specific page."""
    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        page = await handle.query(DocumentPipelineWorkflow.get_page, page_num)
        if page:
            return page
    except Exception:
        pass  # Fall back to SQLite

    # Fall back to SQLite for completed/unavailable workflows
    page = db.get_page(workflow_id, page_num)
    if page:
        return page

    raise HTTPException(404, f"Page {page_num} not found")


@app.patch("/documents/{workflow_id}/pages/{page_num}")
async def update_page(workflow_id: str, data: PageUpdate, page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)")):
    """Update a page (edit markdown, mark reviewed)."""
    old_page = None
    use_sqlite = False

    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        old_page = await handle.query(DocumentPipelineWorkflow.get_page, page_num)

        await handle.signal(
            DocumentPipelineWorkflow.update_page,
            page_num,
            data.edited_markdown,
            data.is_reviewed,
            data.reviewer_notes
        )
    except Exception:
        # Fall back to SQLite for completed/unavailable workflows
        use_sqlite = True
        old_page = db.get_page(workflow_id, page_num)
        if not old_page:
            raise HTTPException(404, f"Page {page_num} not found")

        updated = db.update_page(
            workflow_id,
            page_num,
            edited_markdown=data.edited_markdown,
            is_reviewed=data.is_reviewed,
            reviewer_notes=data.reviewer_notes
        )
        if not updated:
            raise HTTPException(404, f"Page {page_num} not found")

    # Log audits for changed fields
    if data.edited_markdown is not None:
        old_text = old_page.get("edited_markdown") or old_page.get("original_markdown", "")
        _log_audit(
            workflow_id=workflow_id,
            action_type="page_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="edited_markdown",
            old_value=old_text,
            new_value=data.edited_markdown
        )

    if data.is_reviewed is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="page_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="is_reviewed",
            old_value=old_page.get("is_reviewed", False),
            new_value=data.is_reviewed
        )

    if data.reviewer_notes is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="page_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="reviewer_notes",
            old_value=old_page.get("reviewer_notes"),
            new_value=data.reviewer_notes
        )

    # Return updated page
    if use_sqlite:
        return db.get_page(workflow_id, page_num)
    else:
        page = await handle.query(DocumentPipelineWorkflow.get_page, page_num)
        return page


@app.post("/documents/{workflow_id}/pages/{page_num}/reset")
async def reset_page(workflow_id: str, page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)")):
    """Reset page to original OCR output."""
    old_page = None
    use_sqlite = False

    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        old_page = await handle.query(DocumentPipelineWorkflow.get_page, page_num)
        await handle.signal(DocumentPipelineWorkflow.reset_page, page_num)
    except Exception:
        # Fall back to SQLite for completed/unavailable workflows
        use_sqlite = True
        old_page = db.get_page(workflow_id, page_num)
        if not old_page:
            raise HTTPException(404, f"Page {page_num} not found")
        db.reset_page(workflow_id, page_num)

    # Log reset action
    _log_audit(
        workflow_id=workflow_id,
        action_type="page_reset",
        entity_type="page",
        entity_id=page_num,
        field_name="edited_markdown",
        old_value=old_page.get("edited_markdown") if old_page else None,
        new_value=None,
        metadata={"reset_to": "original_markdown"}
    )

    # Return updated page
    if use_sqlite:
        return db.get_page(workflow_id, page_num)
    else:
        page = await handle.query(DocumentPipelineWorkflow.get_page, page_num)
        return page


# =============================================================================
# Chunk Routes (Chunk Review)
# =============================================================================

@app.get("/documents/{workflow_id}/chunks")
async def list_chunks(workflow_id: str, include_excluded: bool = False):
    """Get all chunks for a document."""
    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        chunks = await handle.query(DocumentPipelineWorkflow.get_chunks)
        if not include_excluded:
            chunks = [c for c in chunks if not c.get("is_excluded", False)]
        return chunks
    except Exception:
        pass  # Fall back to SQLite

    # Fall back to SQLite for completed/unavailable workflows
    chunks = db.get_chunks(workflow_id, include_excluded=include_excluded)
    if chunks:
        return chunks

    raise HTTPException(404, f"Document not found: {workflow_id}")


@app.get("/documents/{workflow_id}/chunks/{chunk_num}")
async def get_chunk(workflow_id: str, chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)")):
    """Get a specific chunk."""
    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        chunk = await handle.query(DocumentPipelineWorkflow.get_chunk, chunk_num)
        if chunk:
            return chunk
    except Exception:
        pass  # Fall back to SQLite

    # Fall back to SQLite for completed/unavailable workflows
    chunk = db.get_chunk(workflow_id, chunk_num)
    if chunk:
        return chunk

    raise HTTPException(404, f"Chunk {chunk_num} not found")


@app.patch("/documents/{workflow_id}/chunks/{chunk_num}")
async def update_chunk(workflow_id: str, data: ChunkUpdate, chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)")):
    """Update a chunk (edit text, mark reviewed, exclude)."""
    old_chunk = None
    use_sqlite = False

    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        old_chunk = await handle.query(DocumentPipelineWorkflow.get_chunk, chunk_num)

        await handle.signal(
            DocumentPipelineWorkflow.update_chunk,
            chunk_num,
            data.edited_text,
            data.is_reviewed,
            data.is_excluded,
            data.reviewer_notes
        )
    except Exception:
        # Fall back to SQLite for completed/unavailable workflows
        use_sqlite = True
        old_chunk = db.get_chunk(workflow_id, chunk_num)
        if not old_chunk:
            raise HTTPException(404, f"Chunk {chunk_num} not found")

        updated = db.update_chunk(
            workflow_id,
            chunk_num,
            edited_text=data.edited_text,
            is_reviewed=data.is_reviewed,
            is_excluded=data.is_excluded,
            reviewer_notes=data.reviewer_notes
        )
        if not updated:
            raise HTTPException(404, f"Chunk {chunk_num} not found")

    # Log audits for changed fields
    if data.edited_text is not None:
        old_text = old_chunk.get("edited_text") or old_chunk.get("original_text", "")
        _log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="edited_text",
            old_value=old_text,
            new_value=data.edited_text
        )

    if data.is_reviewed is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="is_reviewed",
            old_value=old_chunk.get("is_reviewed", False),
            new_value=data.is_reviewed
        )

    if data.is_excluded is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="is_excluded",
            old_value=old_chunk.get("is_excluded", False),
            new_value=data.is_excluded
        )

        # If excluding a chunk and document is completed (already ingested), remove from Marqo
        if data.is_excluded and not old_chunk.get("is_excluded", False):
            doc = db.get_document(workflow_id)
            if doc and doc.get("stage") == "completed":
                doc_id = doc.get("document_id")
                if doc_id:
                    marqo_result = delete_single_chunk_from_marqo(doc_id, chunk_num)
                    if marqo_result.get("deleted"):
                        _log_audit(
                            workflow_id=workflow_id,
                            action_type="chunk_removed_from_search",
                            entity_type="chunk",
                            entity_id=chunk_num,
                            metadata={"marqo_id": marqo_result.get("chunk_id")}
                        )

    if data.reviewer_notes is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="reviewer_notes",
            old_value=old_chunk.get("reviewer_notes"),
            new_value=data.reviewer_notes
        )

    # Return updated chunk
    if use_sqlite:
        return db.get_chunk(workflow_id, chunk_num)
    else:
        chunk = await handle.query(DocumentPipelineWorkflow.get_chunk, chunk_num)
        return chunk


@app.post("/documents/{workflow_id}/chunks/{chunk_num}/reset")
async def reset_chunk(workflow_id: str, chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)")):
    """Reset chunk to original text."""
    old_chunk = None
    use_sqlite = False

    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        old_chunk = await handle.query(DocumentPipelineWorkflow.get_chunk, chunk_num)
        await handle.signal(DocumentPipelineWorkflow.reset_chunk, chunk_num)
    except Exception:
        # Fall back to SQLite for completed/unavailable workflows
        use_sqlite = True
        old_chunk = db.get_chunk(workflow_id, chunk_num)
        if not old_chunk:
            raise HTTPException(404, f"Chunk {chunk_num} not found")
        db.reset_chunk(workflow_id, chunk_num)

    # Log reset action
    _log_audit(
        workflow_id=workflow_id,
        action_type="chunk_reset",
        entity_type="chunk",
        entity_id=chunk_num,
        field_name="edited_text",
        old_value=old_chunk.get("edited_text") if old_chunk else None,
        new_value=None,
        metadata={"reset_to": "original_text"}
    )

    # Return updated chunk
    if use_sqlite:
        return db.get_chunk(workflow_id, chunk_num)
    else:
        chunk = await handle.query(DocumentPipelineWorkflow.get_chunk, chunk_num)
        return chunk


# =============================================================================
# Export Routes
# =============================================================================

@app.get("/documents/{workflow_id}/export/markdown")
async def export_markdown(workflow_id: str):
    """Export document as combined markdown."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query(DocumentPipelineWorkflow.get_state)
        pages = await handle.query(DocumentPipelineWorkflow.get_pages)

        content = []
        for page in pages:
            md = page.get("edited_markdown") or page.get("original_markdown", "")
            content.append(f"<!-- Page {page.get('page_number')} -->\n\n{md}")

        return {
            "filename": state.get("filename", "").replace(".pdf", ".md"),
            "content": "\n\n---\n\n".join(content)
        }
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.get("/documents/{workflow_id}/export/chunks")
async def export_chunks(workflow_id: str, include_excluded: bool = False):
    """Export chunks as JSON for Marqo ingestion."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query(DocumentPipelineWorkflow.get_state)
        chunks = await handle.query(DocumentPipelineWorkflow.get_chunks)

        doc_id = state.get("document_id", "")
        filename = state.get("filename", "")
        name = filename.replace(".pdf", "")

        records = []
        for chunk in chunks:
            if not include_excluded and chunk.get("is_excluded", False):
                continue

            text = chunk.get("edited_text") or chunk.get("original_text", "")
            chunk_num = chunk.get("chunk_number", 0)

            records.append({
                "_id": hashlib.md5(f"{doc_id}_{chunk_num}_{text[:50]}".encode()).hexdigest(),
                "doc_id": doc_id,
                "name": name,
                "text": text,
                "chunk_num": chunk_num,
                "token_count": chunk.get("token_count", 0),
                "source": "documents"
            })

        return records
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


# =============================================================================
# PDF Serving
# =============================================================================

@app.get("/documents/{workflow_id}/pdf")
async def get_document_pdf(workflow_id: str):
    """
    Get the original PDF file for a document.
    Returns the PDF as a streaming response.
    """
    filepath = ""
    filename = "document.pdf"

    # Try Temporal first, fall back to SQLite
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query(DocumentPipelineWorkflow.get_state)
        filepath = state.get("filepath", "")
        filename = state.get("filename", "document.pdf")
    except Exception:
        pass  # Will try SQLite below

    # Fall back to SQLite if Temporal query failed
    if not filepath:
        doc = db.get_document(workflow_id)
        if doc:
            filepath = doc.get("filepath", "")
            filename = doc.get("filename", "document.pdf")

    if not filepath:
        raise HTTPException(404, f"Document not found or no PDF path: {workflow_id}")

    try:
        if filepath.startswith("minio://"):
            # Parse minio://bucket/object/path
            path = filepath.replace("minio://", "")
            parts = path.split("/", 1)
            bucket = parts[0]
            object_name = parts[1] if len(parts) > 1 else ""

            # Get object from MinIO
            response = minio_client.get_object(bucket, object_name)

            return StreamingResponse(
                response,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'inline; filename="{filename}"'
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
                    "Content-Disposition": f'inline; filename="{filename}"'
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        # Log the actual error server-side but don't expose details to client
        logging.error(f"PDF serving error for {workflow_id}: {str(e)}")
        raise HTTPException(500, "Error serving PDF file")


# =============================================================================
# Health
# =============================================================================

@app.get("/health")
async def health():
    """Health check."""
    return {
        "status": "ok",
        "temporal_connected": temporal_client is not None
    }


@app.get("/pipeline/stages")
async def get_pipeline_stages():
    """Get the pipeline stages for UI stepper display."""
    return [
        {"id": stage[0], "label": stage[1], "description": stage[2]}
        for stage in PIPELINE_STAGES
    ]


# =============================================================================
# Settings Routes
# =============================================================================

@app.get("/settings/search", response_model=SearchSettings)
async def get_search_settings():
    """
    Get current search settings.

    Returns the current search configuration including:
    - searchMethod: TENSOR, LEXICAL, or HYBRID
    - limit: Number of results to return
    - alpha: Balance between lexical (0) and semantic (1) for hybrid search
    - rankingMethod: rrf or normalize_linear for hybrid search
    - showHighlights: Whether to show highlighted matches
    - efSearch: HNSW search accuracy parameter
    """
    return db.get_search_settings()


@app.put("/settings/search", response_model=SearchSettings)
async def update_search_settings_endpoint(settings: SearchSettingsUpdate):
    """
    Update search settings.

    Only provided fields will be updated. Changes are logged to the audit trail.
    """
    # Convert to dict, excluding None values
    updates = {k: v for k, v in settings.model_dump().items() if v is not None}

    if not updates:
        raise HTTPException(400, "No settings provided to update")

    return db.update_search_settings(updates)


@app.get("/settings/search/audit", response_model=SettingsAuditResponse)
async def get_search_settings_audit(
    limit: int = Query(50, le=200),
    offset: int = 0
):
    """
    Get audit trail for search settings changes.

    Shows all historical changes to search settings with old/new values.
    """
    logs = db.get_settings_audit_logs(limit=limit, offset=offset)
    total = db.get_settings_audit_count()

    return SettingsAuditResponse(
        logs=logs,
        total=total,
        limit=limit,
        offset=offset
    )


@app.post("/settings/search/reset", response_model=SearchSettings)
async def reset_search_settings():
    """
    Reset search settings to defaults.

    Resets all search settings to their default values:
    - searchMethod: HYBRID
    - limit: 10
    - alpha: 0.7
    - rankingMethod: rrf
    - showHighlights: true
    - efSearch: 256
    """
    defaults = {
        "searchMethod": "HYBRID",
        "limit": 10,
        "alpha": 0.7,
        "rankingMethod": "rrf",
        "showHighlights": True,
        "efSearch": 256,
    }
    return db.update_search_settings(defaults)
