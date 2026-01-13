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
    from .activities import (
        run_ocr, create_chunks, prepare_for_ingestion, ingest_to_marqo,
        update_document_state, detect_and_translate_pages, persist_document_content
    )
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

STATE_UPDATE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_attempts=3,
)

TRANSLATION_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=3,
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
    translation_approved: bool = False
    chunks_approved: bool = False
    ingestion_approved: bool = False

    # Translation config
    skip_translation: bool = False  # Set True to skip translation step
    translation_completed_at: Optional[str] = None

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

            # Update SQLite state before long activity
            await workflow.execute_activity(
                update_document_state,
                args=[workflow.info().workflow_id, "ocr_processing", 0, 0, None],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=STATE_UPDATE_RETRY,
            )

            pages = await workflow.execute_activity(
                run_ocr,
                filepath,
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=OCR_RETRY,
            )

            self.state.pages = pages
            self.state.ocr_completed_at = _now_iso()
            self.state.stage = DocumentStage.OCR_REVIEW

            # Update SQLite after OCR complete
            await workflow.execute_activity(
                update_document_state,
                args=[workflow.info().workflow_id, "ocr_review", len(pages), 0, None],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=STATE_UPDATE_RETRY,
            )

            workflow.logger.info(f"OCR complete: {len(pages)} pages, waiting for approval")

            # =========== Wait for OCR approval ===========
            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.ocr_approved)

            workflow.logger.info("OCR approved, starting translation")

            # =========== Stage 2: Translation ===========
            self.state.stage = DocumentStage.TRANSLATION_PROCESSING

            # Update SQLite state
            await workflow.execute_activity(
                update_document_state,
                args=[workflow.info().workflow_id, "translation_processing", len(self.state.pages), 0, None],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=STATE_UPDATE_RETRY,
            )

            # Detect language and translate non-English pages
            translated_pages = await workflow.execute_activity(
                detect_and_translate_pages,
                args=[self.state.pages],
                start_to_close_timeout=timedelta(minutes=60),  # Translation can take time
                retry_policy=TRANSLATION_RETRY,
            )

            self.state.pages = translated_pages
            self.state.translation_completed_at = _now_iso()
            self.state.stage = DocumentStage.TRANSLATION_REVIEW

            # Update SQLite after translation
            await workflow.execute_activity(
                update_document_state,
                args=[workflow.info().workflow_id, "translation_review", len(self.state.pages), 0, None],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=STATE_UPDATE_RETRY,
            )

            # Count translated pages
            translated_count = sum(1 for p in self.state.pages if p.get("translated_markdown"))
            workflow.logger.info(f"Translation complete: {translated_count} pages translated, waiting for approval")

            # =========== Wait for Translation approval ===========
            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.translation_approved)

            workflow.logger.info("Translation approved, starting chunking")

            # =========== Stage 3: Chunking ===========
            self.state.stage = DocumentStage.CHUNKING

            # Update SQLite state
            await workflow.execute_activity(
                update_document_state,
                args=[workflow.info().workflow_id, "chunking", len(self.state.pages), 0, None],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=STATE_UPDATE_RETRY,
            )

            chunks = await workflow.execute_activity(
                create_chunks,
                args=[self.state.pages, chunk_size, chunk_overlap, min_tokens],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=CHUNK_RETRY,
            )

            self.state.chunks = chunks
            self.state.chunks_completed_at = _now_iso()
            self.state.stage = DocumentStage.CHUNK_REVIEW

            # Update SQLite after chunking
            await workflow.execute_activity(
                update_document_state,
                args=[workflow.info().workflow_id, "chunk_review", len(self.state.pages), len(chunks), None],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=STATE_UPDATE_RETRY,
            )

            workflow.logger.info(f"Chunking complete: {len(chunks)} chunks, waiting for approval")

            # =========== Wait for chunks approval ===========
            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.chunks_approved)

            workflow.logger.info("Chunks approved, preparing for ingestion")

            # =========== Stage 3: Prepare for Ingestion ===========
            self.state.stage = DocumentStage.READY_FOR_INGESTION

            records = await workflow.execute_activity(
                prepare_for_ingestion,
                args=[document_id, filename, self.state.chunks],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=CHUNK_RETRY,
            )

            # Update SQLite - ready for final review
            await workflow.execute_activity(
                update_document_state,
                args=[workflow.info().workflow_id, "ready_for_ingestion", len(self.state.pages), len(self.state.chunks), None],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=STATE_UPDATE_RETRY,
            )

            workflow.logger.info(f"Prepared {len(records)} records, waiting for ingestion approval")

            # =========== Wait for ingestion approval ===========
            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.ingestion_approved)

            workflow.logger.info("Ingestion approved, starting ingestion to Marqo")

            # =========== Stage 4: Ingestion ===========
            self.state.stage = DocumentStage.INGESTING

            # Update SQLite before ingestion
            await workflow.execute_activity(
                update_document_state,
                args=[workflow.info().workflow_id, "ingesting", len(self.state.pages), len(self.state.chunks), None],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=STATE_UPDATE_RETRY,
            )

            result = await workflow.execute_activity(
                ingest_to_marqo,
                args=[records, marqo_url, index_name],
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=INGEST_RETRY,
            )

            self.state.ingested_at = _now_iso()
            self.state.stage = DocumentStage.COMPLETED

            # Update SQLite on completion
            await workflow.execute_activity(
                update_document_state,
                args=[workflow.info().workflow_id, "completed", len(self.state.pages), len(self.state.chunks), None],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=STATE_UPDATE_RETRY,
            )

            # Persist pages and chunks to SQLite for post-workflow editing
            # Pages/chunks may be dicts (from activity) or Pydantic models
            pages_data = [p if isinstance(p, dict) else p.model_dump() for p in self.state.pages]
            chunks_data = [c if isinstance(c, dict) else c.model_dump() for c in self.state.chunks]
            await workflow.execute_activity(
                persist_document_content,
                args=[workflow.info().workflow_id, pages_data, chunks_data],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=STATE_UPDATE_RETRY,
            )

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

            # Update SQLite with failure
            try:
                await workflow.execute_activity(
                    update_document_state,
                    args=[workflow.info().workflow_id, "failed", len(self.state.pages), len(self.state.chunks), str(e)],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=STATE_UPDATE_RETRY,
                )
            except Exception:
                pass  # Don't let state update failure mask the original error

            raise

    # =========== Queries ===========

    @workflow.query
    def get_state(self) -> dict:
        """Get current workflow state."""
        if not self.state:
            return {}
        # Count translated pages
        translated_count = sum(1 for p in self.state.pages if p.get("translated_markdown"))

        return {
            "document_id": self.state.document_id,
            "filename": self.state.filename,
            "filepath": self.state.filepath,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "page_count": len(self.state.pages),
            "chunk_count": len(self.state.chunks),
            "translated_count": translated_count,
            "ocr_approved": self.state.ocr_approved,
            "translation_approved": self.state.translation_approved,
            "chunks_approved": self.state.chunks_approved,
            "ingestion_approved": self.state.ingestion_approved,
            "created_at": self.state.created_at,
            "ocr_completed_at": self.state.ocr_completed_at,
            "translation_completed_at": self.state.translation_completed_at,
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
        """Signal to approve OCR and continue to translation."""
        workflow.logger.info("Received OCR approval signal")
        self.state.ocr_approved = True

    @workflow.signal
    def approve_translation(self):
        """Signal to approve translation and continue to chunking."""
        workflow.logger.info("Received translation approval signal")
        self.state.translation_approved = True

    @workflow.signal
    def approve_chunks(self):
        """Signal to approve chunks and continue to prepare for ingestion."""
        workflow.logger.info("Received chunks approval signal")
        self.state.chunks_approved = True

    @workflow.signal
    def approve_ingestion(self):
        """Signal to approve ingestion and continue to Marqo ingestion."""
        workflow.logger.info("Received ingestion approval signal")
        self.state.ingestion_approved = True

    @workflow.signal
    def update_page(self, page_number: int, edited_markdown: Optional[str] = None,
                    is_reviewed: Optional[bool] = None, reviewer_notes: Optional[str] = None,
                    edited_translation: Optional[str] = None, translation_reviewed: Optional[bool] = None,
                    translation_notes: Optional[str] = None):
        """Signal to update a page (OCR or translation)."""
        for page in self.state.pages:
            if page.get("page_number") == page_number:
                # OCR edits
                if edited_markdown is not None:
                    page["edited_markdown"] = edited_markdown
                if is_reviewed is not None:
                    page["is_reviewed"] = is_reviewed
                if reviewer_notes is not None:
                    page["reviewer_notes"] = reviewer_notes
                # Translation edits
                if edited_translation is not None:
                    page["edited_translation"] = edited_translation
                if translation_reviewed is not None:
                    page["translation_reviewed"] = translation_reviewed
                if translation_notes is not None:
                    page["translation_notes"] = translation_notes
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
