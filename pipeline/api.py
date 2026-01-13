"""
FastAPI REST API for the Temporal-based OCR pipeline.

This API provides HTTP endpoints that interact with Temporal workflows.
"""

import os
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
    ApprovalRequest, DocumentSummary, DocumentStage, PIPELINE_STAGES
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
REST API for the Temporal-based OCR pipeline.

## Workflow Stages

1. `registered` - Document registered
2. `ocr_processing` - OCR in progress
3. `ocr_review` - **Waiting for user review/approval**
4. `chunking` - Chunking in progress
5. `chunk_review` - **Waiting for user review/approval**
6. `ready_for_ingestion` - Preparing records
7. `ingesting` - Ingesting to Marqo
8. `completed` - Done
9. `failed` - Error occurred

## Review Flow

1. Start workflow with `POST /documents`
2. Wait for `ocr_review` stage
3. Review/edit pages with `GET/PATCH /documents/{id}/pages/{num}`
4. Approve with `POST /documents/{id}/approve-ocr`
5. Wait for `chunk_review` stage
6. Review/edit chunks with `GET/PATCH /documents/{id}/chunks/{num}`
7. Approve with `POST /documents/{id}/approve-chunks`
8. Workflow completes automatically
    """,
    version="1.0.0",
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
        return {"approved": "ocr", "workflow_id": workflow_id}
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.post("/documents/{workflow_id}/approve-chunks")
async def approve_chunks(workflow_id: str):
    """Approve chunks and continue to prepare for ingestion."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(DocumentPipelineWorkflow.approve_chunks)
        return {"approved": "chunks", "workflow_id": workflow_id}
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.post("/documents/{workflow_id}/approve-translation")
async def approve_translation(workflow_id: str):
    """Approve translations and continue to chunking."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(DocumentPipelineWorkflow.approve_translation)
        return {"approved": "translation", "workflow_id": workflow_id}
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.post("/documents/{workflow_id}/approve-ingestion")
async def approve_ingestion(workflow_id: str):
    """Approve ingestion and continue to Marqo ingestion."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(DocumentPipelineWorkflow.approve_ingestion)
        return {"approved": "ingestion", "workflow_id": workflow_id}
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


# =============================================================================
# Page Routes (OCR Review)
# =============================================================================

@app.get("/documents/{workflow_id}/pages")
async def list_pages(workflow_id: str):
    """Get all pages for a document."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        pages = await handle.query(DocumentPipelineWorkflow.get_pages)
        return pages
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.get("/documents/{workflow_id}/pages/{page_num}")
async def get_page(workflow_id: str, page_num: int):
    """Get a specific page."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        page = await handle.query(DocumentPipelineWorkflow.get_page, page_num)
        if not page:
            raise HTTPException(404, f"Page {page_num} not found")
        return page
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.patch("/documents/{workflow_id}/pages/{page_num}")
async def update_page(workflow_id: str, page_num: int, data: PageUpdate):
    """Update a page (edit markdown, mark reviewed)."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(
            DocumentPipelineWorkflow.update_page,
            page_num,
            data.edited_markdown,
            data.is_reviewed,
            data.reviewer_notes
        )
        # Return updated page
        page = await handle.query(DocumentPipelineWorkflow.get_page, page_num)
        return page
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.post("/documents/{workflow_id}/pages/{page_num}/reset")
async def reset_page(workflow_id: str, page_num: int):
    """Reset page to original OCR output."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(DocumentPipelineWorkflow.reset_page, page_num)
        page = await handle.query(DocumentPipelineWorkflow.get_page, page_num)
        return page
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


# =============================================================================
# Chunk Routes (Chunk Review)
# =============================================================================

@app.get("/documents/{workflow_id}/chunks")
async def list_chunks(workflow_id: str, include_excluded: bool = False):
    """Get all chunks for a document."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        chunks = await handle.query(DocumentPipelineWorkflow.get_chunks)
        if not include_excluded:
            chunks = [c for c in chunks if not c.get("is_excluded", False)]
        return chunks
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.get("/documents/{workflow_id}/chunks/{chunk_num}")
async def get_chunk(workflow_id: str, chunk_num: int):
    """Get a specific chunk."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        chunk = await handle.query(DocumentPipelineWorkflow.get_chunk, chunk_num)
        if not chunk:
            raise HTTPException(404, f"Chunk {chunk_num} not found")
        return chunk
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.patch("/documents/{workflow_id}/chunks/{chunk_num}")
async def update_chunk(workflow_id: str, chunk_num: int, data: ChunkUpdate):
    """Update a chunk (edit text, mark reviewed, exclude)."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(
            DocumentPipelineWorkflow.update_chunk,
            chunk_num,
            data.edited_text,
            data.is_reviewed,
            data.is_excluded,
            data.reviewer_notes
        )
        chunk = await handle.query(DocumentPipelineWorkflow.get_chunk, chunk_num)
        return chunk
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


@app.post("/documents/{workflow_id}/chunks/{chunk_num}/reset")
async def reset_chunk(workflow_id: str, chunk_num: int):
    """Reset chunk to original text."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.signal(DocumentPipelineWorkflow.reset_chunk, chunk_num)
        chunk = await handle.query(DocumentPipelineWorkflow.get_chunk, chunk_num)
        return chunk
    except Exception as e:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


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

        # Step 5: Verify results
        if test_results["success"]:
            try:
                pages = await handle.query(DocumentPipelineWorkflow.get_pages)
                chunks = await handle.query(DocumentPipelineWorkflow.get_chunks)

                test_results["verification"] = {
                    "page_count": len(pages),
                    "chunk_count": len(chunks),
                    "has_pages": len(pages) > 0,
                    "has_chunks": len(chunks) > 0,
                    "sample_chunk": chunks[0].get("original_text", "")[:200] if chunks else None
                }

                # Check if translation happened (for non-English docs)
                translated_count = sum(1 for p in pages if p.get("translated_markdown"))
                test_results["verification"]["translated_pages"] = translated_count

            except Exception as e:
                test_results["errors"].append(f"Verification failed: {str(e)}")

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
