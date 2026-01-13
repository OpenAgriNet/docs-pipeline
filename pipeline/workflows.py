"""
Temporal workflows for the OCR pipeline.

The workflow pauses at review stages, waiting for user signals to continue.
"""

from datetime import timedelta
from dataclasses import dataclass, field
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .activities import run_ocr, create_chunks, prepare_for_ingestion, ingest_to_marqo
    from .models import DocumentStage, PageData, ChunkData


def _now_iso() -> str:
    """Get current time as ISO string (workflow-safe)."""
    return workflow.now().isoformat()


# Retry policies
OCR_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
)

CHUNK_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=3,
)

INGEST_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=5,
)


@dataclass
class DocumentWorkflowState:
    """Workflow state - queryable and modifiable via signals."""
    document_id: str
    filename: str
    filepath: str

    stage: DocumentStage = DocumentStage.REGISTERED
    error_message: Optional[str] = None

    pages: list[dict] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)

    # Approval flags
    ocr_approved: bool = False
    chunks_approved: bool = False

    # Config
    chunk_size: int = 450
    chunk_overlap: int = 128
    min_tokens: int = 100
    marqo_url: str = ""  # Empty = use MARQO_URL env var
    index_name: str = "documents-index"

    # Timestamps
    created_at: str = ""
    ocr_completed_at: Optional[str] = None
    chunks_completed_at: Optional[str] = None
    ingested_at: Optional[str] = None


@workflow.defn
class DocumentPipelineWorkflow:
    """
    Main document processing workflow.

    Flow:
    1. Start → OCR processing
    2. OCR complete → Wait for approval signal
    3. Approved → Chunking
    4. Chunking complete → Wait for approval signal
    5. Approved → Ingestion
    6. Done

    Users can edit pages/chunks while waiting for approval.
    """

    def __init__(self):
        self.state = None

    @workflow.run
    async def run(
        self,
        document_id: str,
        filename: str,
        filepath: str,
        chunk_size: int = 450,
        chunk_overlap: int = 128,
        min_tokens: int = 100,
        marqo_url: str = "",  # Empty = use MARQO_URL env var
        index_name: str = "documents-index",
        auto_approve: bool = False  # Skip manual review if True
    ) -> dict:
        """Run the document pipeline."""

        self.state = DocumentWorkflowState(
            document_id=document_id,
            filename=filename,
            filepath=filepath,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            min_tokens=min_tokens,
            marqo_url=marqo_url,
            index_name=index_name,
            created_at=_now_iso()
        )

        try:
            # =========== Stage 1: OCR ===========
            self.state.stage = DocumentStage.OCR_PROCESSING
            workflow.logger.info(f"Starting OCR for {filename}")

            pages = await workflow.execute_activity(
                run_ocr,
                filepath,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=OCR_RETRY,
            )

            self.state.pages = pages
            self.state.ocr_completed_at = _now_iso()
            self.state.stage = DocumentStage.OCR_REVIEW

            workflow.logger.info(f"OCR complete: {len(pages)} pages, waiting for approval")

            # =========== Wait for OCR approval ===========
            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.ocr_approved)

            workflow.logger.info("OCR approved, starting chunking")

            # =========== Stage 2: Chunking ===========
            self.state.stage = DocumentStage.CHUNKING

            chunks = await workflow.execute_activity(
                create_chunks,
                args=[self.state.pages, chunk_size, chunk_overlap, min_tokens],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=CHUNK_RETRY,
            )

            self.state.chunks = chunks
            self.state.chunks_completed_at = _now_iso()
            self.state.stage = DocumentStage.CHUNK_REVIEW

            workflow.logger.info(f"Chunking complete: {len(chunks)} chunks, waiting for approval")

            # =========== Wait for chunks approval ===========
            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.chunks_approved)

            workflow.logger.info("Chunks approved, preparing for ingestion")

            # =========== Stage 3: Ingestion ===========
            self.state.stage = DocumentStage.READY_FOR_INGESTION

            records = await workflow.execute_activity(
                prepare_for_ingestion,
                args=[document_id, filename, self.state.chunks],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=CHUNK_RETRY,
            )

            self.state.stage = DocumentStage.INGESTING

            result = await workflow.execute_activity(
                ingest_to_marqo,
                args=[records, marqo_url, index_name],
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=INGEST_RETRY,
            )

            self.state.ingested_at = _now_iso()
            self.state.stage = DocumentStage.COMPLETED

            workflow.logger.info(f"Pipeline complete for {filename}")

            return {
                "document_id": document_id,
                "filename": filename,
                "stage": self.state.stage.value,
                "pages": len(self.state.pages),
                "chunks": len(self.state.chunks),
                "records_ingested": result.get("records_ingested", 0)
            }

        except Exception as e:
            self.state.stage = DocumentStage.FAILED
            self.state.error_message = str(e)
            workflow.logger.error(f"Pipeline failed: {e}")
            raise

    # =========== Queries ===========

    @workflow.query
    def get_state(self) -> dict:
        """Get current workflow state."""
        if not self.state:
            return {}
        return {
            "document_id": self.state.document_id,
            "filename": self.state.filename,
            "filepath": self.state.filepath,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "page_count": len(self.state.pages),
            "chunk_count": len(self.state.chunks),
            "ocr_approved": self.state.ocr_approved,
            "chunks_approved": self.state.chunks_approved,
            "created_at": self.state.created_at,
            "ocr_completed_at": self.state.ocr_completed_at,
            "chunks_completed_at": self.state.chunks_completed_at,
            "ingested_at": self.state.ingested_at,
        }

    @workflow.query
    def get_pages(self) -> list[dict]:
        """Get all pages."""
        return self.state.pages if self.state else []

    @workflow.query
    def get_page(self, page_number: int) -> Optional[dict]:
        """Get a specific page."""
        if not self.state:
            return None
        for page in self.state.pages:
            if page.get("page_number") == page_number:
                return page
        return None

    @workflow.query
    def get_chunks(self) -> list[dict]:
        """Get all chunks."""
        return self.state.chunks if self.state else []

    @workflow.query
    def get_chunk(self, chunk_number: int) -> Optional[dict]:
        """Get a specific chunk."""
        if not self.state:
            return None
        for chunk in self.state.chunks:
            if chunk.get("chunk_number") == chunk_number:
                return chunk
        return None

    # =========== Signals (mutations) ===========

    @workflow.signal
    def approve_ocr(self):
        """Signal to approve OCR and continue to chunking."""
        workflow.logger.info("Received OCR approval signal")
        self.state.ocr_approved = True

    @workflow.signal
    def approve_chunks(self):
        """Signal to approve chunks and continue to ingestion."""
        workflow.logger.info("Received chunks approval signal")
        self.state.chunks_approved = True

    @workflow.signal
    def update_page(self, page_number: int, edited_markdown: Optional[str] = None,
                    is_reviewed: Optional[bool] = None, reviewer_notes: Optional[str] = None):
        """Signal to update a page."""
        for page in self.state.pages:
            if page.get("page_number") == page_number:
                if edited_markdown is not None:
                    page["edited_markdown"] = edited_markdown
                if is_reviewed is not None:
                    page["is_reviewed"] = is_reviewed
                if reviewer_notes is not None:
                    page["reviewer_notes"] = reviewer_notes
                workflow.logger.info(f"Updated page {page_number}")
                return
        workflow.logger.warning(f"Page {page_number} not found")

    @workflow.signal
    def update_chunk(self, chunk_number: int, edited_text: Optional[str] = None,
                     is_reviewed: Optional[bool] = None, is_excluded: Optional[bool] = None,
                     reviewer_notes: Optional[str] = None):
        """Signal to update a chunk."""
        for chunk in self.state.chunks:
            if chunk.get("chunk_number") == chunk_number:
                if edited_text is not None:
                    chunk["edited_text"] = edited_text
                    # Recalculate token count
                    from .activities import count_tokens
                    chunk["token_count"] = count_tokens(edited_text)
                if is_reviewed is not None:
                    chunk["is_reviewed"] = is_reviewed
                if is_excluded is not None:
                    chunk["is_excluded"] = is_excluded
                if reviewer_notes is not None:
                    chunk["reviewer_notes"] = reviewer_notes
                workflow.logger.info(f"Updated chunk {chunk_number}")
                return
        workflow.logger.warning(f"Chunk {chunk_number} not found")

    @workflow.signal
    def reset_page(self, page_number: int):
        """Signal to reset a page to original."""
        for page in self.state.pages:
            if page.get("page_number") == page_number:
                page["edited_markdown"] = None
                page["is_reviewed"] = False
                page["reviewer_notes"] = None
                workflow.logger.info(f"Reset page {page_number}")
                return

    @workflow.signal
    def reset_chunk(self, chunk_number: int):
        """Signal to reset a chunk to original."""
        for chunk in self.state.chunks:
            if chunk.get("chunk_number") == chunk_number:
                chunk["edited_text"] = None
                chunk["is_reviewed"] = False
                chunk["is_excluded"] = False
                chunk["reviewer_notes"] = None
                # Reset token count
                from .activities import count_tokens
                chunk["token_count"] = count_tokens(chunk.get("original_text", ""))
                workflow.logger.info(f"Reset chunk {chunk_number}")
                return
