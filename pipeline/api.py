"""
FastAPI REST API for the Temporal-based OCR pipeline.

This API provides HTTP endpoints that interact with Temporal workflows.
"""

import os
import json
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager
from io import BytesIO

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from temporalio.client import Client
from minio import Minio

from .models import (
    RegisterRequest, RegisterFolderRequest, PageUpdate, ChunkUpdate,
    ApprovalRequest, DocumentSummary, DocumentStage, PIPELINE_STAGES,
    AuditLogResponse
)
from .workflows import DocumentPipelineWorkflow
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

    # MinIO
    minio_endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    minio_access_key = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
    minio_secret_key = os.environ.get("MINIO_SECRET_KEY", "minioadmin123")
    MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "documents")

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_workflow_id(filepath: str) -> str:
    """Generate consistent workflow ID from filepath."""
    return f"doc-{hashlib.md5(filepath.encode()).hexdigest()[:12]}"


# =============================================================================
# Document Routes
# =============================================================================

@app.post("/documents", response_model=DocumentSummary)
async def start_document_workflow(
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
    """
    filepath = Path(data.filepath)
    if not filepath.exists():
        raise HTTPException(404, f"File not found: {data.filepath}")

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
    except:
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
async def upload_and_process(
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
    """
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(400, "Only PDF files are allowed")

    # Read file content
    content = await file.read()
    file_size = len(content)

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
    except:
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
async def start_batch_workflows(
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
            results.append(DocumentSummary(
                document_id=hashlib.md5(str(pdf_path).encode()).hexdigest(),
                workflow_id=get_workflow_id(str(pdf_path)),
                filename=pdf_path.name,
                stage=DocumentStage.FAILED,
                page_count=0,
                chunk_count=0,
                error_message=str(e)
            ))

    return results


@app.get("/documents", response_model=list[DocumentSummary])
async def list_documents(
    stage: Optional[DocumentStage] = None,
    limit: int = Query(100, le=500)
):
    """
    List all document workflows.

    Uses SQLite for fast listing with Temporal queries for real-time updates.
    """
    # Get documents from SQLite (always available)
    stage_filter = stage.value if stage else None
    docs = db.list_documents(stage=stage_filter, limit=limit)

    results = []
    for doc in docs:
        workflow_id = doc["workflow_id"]

        # Try to get latest state from Temporal (may fail during activities)
        try:
            handle = temporal_client.get_workflow_handle(workflow_id)
            state = await handle.query(DocumentPipelineWorkflow.get_state)

            if state:
                # Update SQLite with latest state
                db.update_document_stage(
                    workflow_id=workflow_id,
                    stage=state.get("stage", doc["stage"]),
                    page_count=state.get("page_count", 0),
                    chunk_count=state.get("chunk_count", 0),
                    error_message=state.get("error_message")
                )

                doc_stage = DocumentStage(state.get("stage", "registered"))

                # Skip if filtering by stage and doesn't match
                if stage and doc_stage != stage:
                    continue

                results.append(DocumentSummary(
                    document_id=state.get("document_id", doc["document_id"]),
                    workflow_id=workflow_id,
                    filename=state.get("filename", doc["filename"]),
                    stage=doc_stage,
                    page_count=state.get("page_count", 0),
                    chunk_count=state.get("chunk_count", 0),
                    error_message=state.get("error_message")
                ))
                continue
        except:
            pass  # Temporal query failed, use SQLite data

        # Fall back to SQLite data
        doc_stage = DocumentStage(doc["stage"])
        if stage and doc_stage != stage:
            continue

        results.append(DocumentSummary(
            document_id=doc["document_id"],
            workflow_id=workflow_id,
            filename=doc["filename"],
            stage=doc_stage,
            page_count=doc["page_count"],
            chunk_count=doc["chunk_count"],
            error_message=doc["error_message"]
        ))

    return results


@app.get("/documents/{workflow_id}")
async def get_document(workflow_id: str):
    """Get document workflow state."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query(DocumentPipelineWorkflow.get_state)
        return state
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.delete("/documents/{workflow_id}")
async def cancel_document(workflow_id: str):
    """Cancel a document workflow."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.cancel()
        return {"cancelled": workflow_id}
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


# =============================================================================
# Approval Routes
# =============================================================================

@app.post("/documents/{workflow_id}/approve-ocr")
async def approve_ocr(workflow_id: str):
    """Approve OCR results and continue to chunking."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
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
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.post("/documents/{workflow_id}/approve-chunks")
async def approve_chunks(workflow_id: str):
    """Approve chunks and continue to prepare for ingestion."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
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
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.post("/documents/{workflow_id}/approve-translation")
async def approve_translation(workflow_id: str):
    """Approve translations and continue to chunking."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
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
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.post("/documents/{workflow_id}/approve-ingestion")
async def approve_ingestion(workflow_id: str):
    """Approve ingestion and continue to Marqo ingestion."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
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
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


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
    except:
        pass  # Fall back to SQLite

    # Fall back to SQLite for completed/unavailable workflows
    pages = db.get_pages(workflow_id)
    if pages:
        return pages

    raise HTTPException(404, f"Document not found: {workflow_id}")


@app.get("/documents/{workflow_id}/pages/{page_num}")
async def get_page(workflow_id: str, page_num: int):
    """Get a specific page."""
    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        page = await handle.query(DocumentPipelineWorkflow.get_page, page_num)
        if page:
            return page
    except:
        pass  # Fall back to SQLite

    # Fall back to SQLite for completed/unavailable workflows
    page = db.get_page(workflow_id, page_num)
    if page:
        return page

    raise HTTPException(404, f"Page {page_num} not found")


@app.patch("/documents/{workflow_id}/pages/{page_num}")
async def update_page(workflow_id: str, page_num: int, data: PageUpdate):
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
    except:
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
async def reset_page(workflow_id: str, page_num: int):
    """Reset page to original OCR output."""
    old_page = None
    use_sqlite = False

    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        old_page = await handle.query(DocumentPipelineWorkflow.get_page, page_num)
        await handle.signal(DocumentPipelineWorkflow.reset_page, page_num)
    except:
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
    except:
        pass  # Fall back to SQLite

    # Fall back to SQLite for completed/unavailable workflows
    chunks = db.get_chunks(workflow_id, include_excluded=include_excluded)
    if chunks:
        return chunks

    raise HTTPException(404, f"Document not found: {workflow_id}")


@app.get("/documents/{workflow_id}/chunks/{chunk_num}")
async def get_chunk(workflow_id: str, chunk_num: int):
    """Get a specific chunk."""
    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        chunk = await handle.query(DocumentPipelineWorkflow.get_chunk, chunk_num)
        if chunk:
            return chunk
    except:
        pass  # Fall back to SQLite

    # Fall back to SQLite for completed/unavailable workflows
    chunk = db.get_chunk(workflow_id, chunk_num)
    if chunk:
        return chunk

    raise HTTPException(404, f"Chunk {chunk_num} not found")


@app.patch("/documents/{workflow_id}/chunks/{chunk_num}")
async def update_chunk(workflow_id: str, chunk_num: int, data: ChunkUpdate):
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
    except:
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
async def reset_chunk(workflow_id: str, chunk_num: int):
    """Reset chunk to original text."""
    old_chunk = None
    use_sqlite = False

    # Try Temporal first
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        old_chunk = await handle.query(DocumentPipelineWorkflow.get_chunk, chunk_num)
        await handle.signal(DocumentPipelineWorkflow.reset_chunk, chunk_num)
    except:
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
    except:
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
        raise HTTPException(500, f"Error serving PDF: {str(e)}")


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
# E2E Test
# =============================================================================

@app.post("/test/e2e")
async def run_e2e_test(
    file: UploadFile = File(None),
    timeout_seconds: int = 300,
    poll_interval: int = 5
):
    """
    Run an end-to-end pipeline test.

    - Uploads a test PDF (or uses provided file)
    - Runs full pipeline with auto_approve=true
    - Polls until completion or timeout
    - Returns verification results

    If no file provided, uses a small built-in test (requires test PDF in /app/books/).
    """
    import asyncio
    import time

    start_time = time.time()
    test_results = {
        "test_name": "e2e_pipeline_test",
        "started_at": datetime.utcnow().isoformat(),
        "stages_passed": [],
        "stages_failed": [],
        "errors": [],
        "duration_seconds": 0,
        "success": False
    }

    try:
        # Step 1: Get or create test file
        if file:
            content = await file.read()
            filename = file.filename
            test_results["test_file"] = filename
        else:
            # Look for a small test PDF
            test_paths = [
                "/app/test_data/test_small.pdf",  # 185K - bundled test file
                "/app/books/vol_i-1_tb.pdf",
                "./test_data/test_small.pdf",
                "./books/vol_i-1_tb.pdf",
            ]
            test_path = None
            for p in test_paths:
                if Path(p).exists():
                    test_path = p
                    break

            if not test_path:
                test_results["errors"].append("No test PDF found. Upload a file or place a PDF in /app/books/")
                return test_results

            with open(test_path, 'rb') as f:
                content = f.read()
            filename = Path(test_path).name
            test_results["test_file"] = filename

        test_results["stages_passed"].append("file_loaded")

        # Step 2: Upload to MinIO
        file_hash = hashlib.md5(content).hexdigest()[:8] + "_test"
        object_name = f"test/{file_hash}/{filename}"

        minio_client.put_object(
            MINIO_BUCKET,
            object_name,
            BytesIO(content),
            length=len(content),
            content_type="application/pdf"
        )

        minio_path = f"minio://{MINIO_BUCKET}/{object_name}"
        test_results["stages_passed"].append("uploaded_to_minio")
        test_results["minio_path"] = minio_path

        # Step 3: Start workflow with auto_approve
        workflow_id = f"test-{file_hash}-{int(time.time())}"
        document_id = file_hash

        handle = await temporal_client.start_workflow(
            DocumentPipelineWorkflow.run,
            args=[
                document_id,
                filename,
                minio_path,
                450,  # chunk_size
                128,  # chunk_overlap
                100,  # min_tokens
                "",   # marqo_url (use env)
                "documents-index",
                True  # auto_approve
            ],
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )

        test_results["workflow_id"] = workflow_id
        test_results["stages_passed"].append("workflow_started")

        # Step 4: Poll for completion
        last_stage = "registered"
        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout_seconds:
                test_results["errors"].append(f"Timeout after {timeout_seconds}s at stage: {last_stage}")
                test_results["stages_failed"].append("timeout")
                break

            try:
                state = await handle.query(DocumentPipelineWorkflow.get_state)
                current_stage = state.get("stage", "unknown")

                if current_stage != last_stage:
                    test_results["stages_passed"].append(current_stage)
                    last_stage = current_stage

                if current_stage == "completed":
                    test_results["success"] = True
                    test_results["final_state"] = state
                    break
                elif current_stage == "failed":
                    test_results["errors"].append(state.get("error_message", "Unknown error"))
                    test_results["stages_failed"].append("pipeline_failed")
                    test_results["final_state"] = state
                    break

            except Exception as e:
                # Query might fail during activity execution
                pass

            await asyncio.sleep(poll_interval)

        # Step 5: Comprehensive API verification
        if test_results["success"]:
            test_results["api_verification"] = {}
            verification_errors = []

            try:
                # 5a. Verify pages API response structure
                pages = await handle.query(DocumentPipelineWorkflow.get_pages)
                page_verification = {
                    "page_count": len(pages),
                    "has_pages": len(pages) > 0,
                    "required_fields_present": True,
                    "field_errors": []
                }
                required_page_fields = ["page_number", "original_markdown", "is_reviewed"]
                for i, page in enumerate(pages):
                    for field in required_page_fields:
                        if field not in page:
                            page_verification["field_errors"].append(f"Page {i}: missing '{field}'")
                            page_verification["required_fields_present"] = False
                if not page_verification["required_fields_present"]:
                    verification_errors.append(f"Page API missing fields: {page_verification['field_errors']}")
                test_results["api_verification"]["pages"] = page_verification

                # 5b. Verify chunks API response structure
                chunks = await handle.query(DocumentPipelineWorkflow.get_chunks)
                chunk_verification = {
                    "chunk_count": len(chunks),
                    "has_chunks": len(chunks) > 0,
                    "required_fields_present": True,
                    "field_errors": []
                }
                required_chunk_fields = ["chunk_number", "original_text", "token_count", "page_start", "page_end"]
                for i, chunk in enumerate(chunks):
                    for field in required_chunk_fields:
                        if field not in chunk:
                            chunk_verification["field_errors"].append(f"Chunk {i}: missing '{field}'")
                            chunk_verification["required_fields_present"] = False
                if not chunk_verification["required_fields_present"]:
                    verification_errors.append(f"Chunk API missing fields: {chunk_verification['field_errors']}")
                test_results["api_verification"]["chunks"] = chunk_verification

                # 5c. Verify translation (for non-English documents)
                translation_verification = {
                    "pages_detected_non_english": 0,
                    "pages_translated": 0,
                    "chunks_contain_translated_text": True,
                    "sample_languages": []
                }
                for page in pages:
                    lang = page.get("detected_language")
                    if lang:
                        translation_verification["sample_languages"].append(lang)
                    if lang and lang != "en":
                        translation_verification["pages_detected_non_english"] += 1
                        if page.get("translated_markdown"):
                            translation_verification["pages_translated"] += 1

                # If translation happened, verify chunks contain translated text
                if translation_verification["pages_detected_non_english"] > 0:
                    # Get first translated page to compare
                    translated_pages = [p for p in pages if p.get("translated_markdown")]
                    if translated_pages:
                        # Check that chunk text matches translated content, not original
                        original_texts = set()
                        translated_texts = set()
                        for p in pages:
                            if p.get("original_markdown"):
                                # Extract first 50 chars as fingerprint
                                original_texts.add(p["original_markdown"][:50] if len(p["original_markdown"]) > 50 else p["original_markdown"])
                            if p.get("translated_markdown"):
                                translated_texts.add(p["translated_markdown"][:50] if len(p["translated_markdown"]) > 50 else p["translated_markdown"])

                        # Check if any chunk starts with original (non-translated) text
                        for chunk in chunks:
                            chunk_text = chunk.get("original_text", "")[:50]
                            # If chunk text matches original non-English and not translated, that's an error
                            for orig in original_texts:
                                if chunk_text.startswith(orig[:30]) and orig not in translated_texts:
                                    translation_verification["chunks_contain_translated_text"] = False
                                    verification_errors.append("Chunks contain original language text instead of translated text")
                                    break

                test_results["api_verification"]["translation"] = translation_verification

                # 5d. Verify state API response structure
                # Note: pages and chunks are queried separately via get_pages/get_chunks
                state = await handle.query(DocumentPipelineWorkflow.get_state)
                state_verification = {
                    "required_fields_present": True,
                    "field_errors": []
                }
                required_state_fields = ["document_id", "filename", "filepath", "stage"]
                for field in required_state_fields:
                    if field not in state:
                        state_verification["field_errors"].append(f"State missing '{field}'")
                        state_verification["required_fields_present"] = False
                if not state_verification["required_fields_present"]:
                    verification_errors.append(f"State API missing fields: {state_verification['field_errors']}")
                test_results["api_verification"]["state"] = state_verification

                # 5e. Check if chunks contain non-Latin script (indicates untranslated text)
                def contains_non_latin(text):
                    """Check if text contains significant non-Latin script (Gujarati, Hindi, etc.)"""
                    import unicodedata
                    non_latin_count = 0
                    total_alpha = 0
                    for char in text:
                        if char.isalpha():
                            total_alpha += 1
                            # Check if character is from a non-Latin script
                            script = unicodedata.name(char, '').split()[0]
                            if script in ['GUJARATI', 'DEVANAGARI', 'BENGALI', 'TAMIL', 'TELUGU', 'KANNADA', 'MALAYALAM', 'ORIYA', 'GURMUKHI']:
                                non_latin_count += 1
                    return total_alpha > 0 and (non_latin_count / total_alpha) > 0.1  # More than 10% non-Latin

                chunks_with_non_latin = []
                for i, chunk in enumerate(chunks):
                    chunk_text = chunk.get("original_text", "")
                    if contains_non_latin(chunk_text):
                        chunks_with_non_latin.append(i + 1)

                if chunks_with_non_latin:
                    translation_verification["chunks_contain_untranslated_text"] = True
                    translation_verification["chunks_with_non_latin_script"] = chunks_with_non_latin
                    verification_errors.append(f"Chunks {chunks_with_non_latin} contain non-Latin script (possibly untranslated)")

                # 5f. Sample data for debugging
                test_results["api_verification"]["sample_data"] = {
                    "first_chunk_preview": chunks[0].get("original_text", "")[:200] if chunks else None,
                    "first_page_language": pages[0].get("detected_language") if pages else None,
                    "first_page_has_translation": bool(pages[0].get("translated_markdown")) if pages else False
                }

                # Summary
                test_results["api_verification"]["all_passed"] = len(verification_errors) == 0
                test_results["api_verification"]["errors"] = verification_errors
                if verification_errors:
                    test_results["errors"].extend(verification_errors)

            except Exception as e:
                test_results["errors"].append(f"API verification failed: {str(e)}")

    except Exception as e:
        test_results["errors"].append(str(e))
        test_results["stages_failed"].append("exception")

    test_results["duration_seconds"] = round(time.time() - start_time, 2)
    test_results["completed_at"] = datetime.utcnow().isoformat()

    return test_results


@app.get("/test/status/{workflow_id}")
async def get_test_status(workflow_id: str):
    """Get the status of a test workflow."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query(DocumentPipelineWorkflow.get_state)
        return state
    except Exception as e:
        raise HTTPException(404, f"Test workflow not found: {workflow_id}")
