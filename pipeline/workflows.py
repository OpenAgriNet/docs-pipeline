"""
Temporal workflows for the OCR pipeline.

The workflow pauses at review stages, waiting for user signals to continue.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .activities import (
        auto_tag_chunks_from_db,
        create_chunks_from_db,
        detect_and_translate_pages_from_db,
        ingest_document_from_db,
        run_ocr_and_store,
        update_document_state,
    )
    from .models import DocumentStage


def _now_iso() -> str:
    """Get current time as ISO string (workflow-safe)."""
    return workflow.now().isoformat()


OCR_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=15),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=15),
    maximum_attempts=20,
)

CHUNK_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=1.5,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=10,
)

INGEST_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=10),
    maximum_attempts=15,
)

STATE_UPDATE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=3),
    backoff_coefficient=1.5,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=10,
)

TRANSLATION_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=15),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=15),
    maximum_attempts=20,
)


async def _mirror_state(
    workflow_id: str,
    stage: str,
    page_count: int = 0,
    chunk_count: int = 0,
    error_message: Optional[str] = None,
) -> dict:
    """
    Mirror workflow state into SQLite as a local activity.

    Local activities avoid starving tiny state updates behind long-running OCR,
    translation, or chunking activities on the shared task queue.
    """
    if workflow.patched("local-state-update-v1"):
        return await workflow.execute_local_activity(
            update_document_state,
            args=[workflow_id, stage, page_count, chunk_count, error_message],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=STATE_UPDATE_RETRY,
        )

    return await workflow.execute_activity(
        update_document_state,
        args=[workflow_id, stage, page_count, chunk_count, error_message],
        start_to_close_timeout=timedelta(seconds=30),
        retry_policy=STATE_UPDATE_RETRY,
    )


@dataclass
class DocumentWorkflowState:
    """Workflow state - queryable and modifiable via signals."""

    document_id: str
    filename: str
    filepath: str

    stage: DocumentStage = DocumentStage.REGISTERED
    error_message: Optional[str] = None

    page_count: int = 0
    chunk_count: int = 0
    translated_count: int = 0

    ocr_approved: bool = False
    translation_approved: bool = False
    chunks_approved: bool = False
    ingestion_approved: bool = False

    chunk_size: int = 450
    chunk_overlap: int = 128
    min_tokens: int = 100
    marqo_url: str = ""
    index_name: str = "documents-index"
    stop_after_ocr: bool = False

    created_at: str = ""
    ocr_completed_at: Optional[str] = None
    translation_completed_at: Optional[str] = None
    chunks_completed_at: Optional[str] = None
    ingested_at: Optional[str] = None


@workflow.defn
class DocumentPipelineWorkflow:
    """Main document processing workflow."""

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
        marqo_url: str = "",
        index_name: str = "documents-index",
        auto_approve: bool = False,
        stop_after_ocr: bool = False,
    ) -> dict:
        self.state = DocumentWorkflowState(
            document_id=document_id,
            filename=filename,
            filepath=filepath,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            min_tokens=min_tokens,
            marqo_url=marqo_url,
            index_name=index_name,
            stop_after_ocr=stop_after_ocr,
            created_at=_now_iso(),
        )

        try:
            self.state.stage = DocumentStage.OCR_PROCESSING
            workflow.logger.info(f"Starting OCR for {filename}")

            await _mirror_state(workflow.info().workflow_id, "ocr_processing", 0, 0, None)

            ocr_result = await workflow.execute_activity(
                run_ocr_and_store,
                args=[workflow.info().workflow_id, filepath],
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=OCR_RETRY,
            )
            self.state.page_count = ocr_result.get("page_count", 0)
            self.state.ocr_completed_at = _now_iso()
            self.state.stage = DocumentStage.OCR_REVIEW

            await _mirror_state(workflow.info().workflow_id, "ocr_review", self.state.page_count, 0, None)

            if stop_after_ocr:
                workflow.logger.info(f"OCR-only run complete for {filename}")
                return {
                    "document_id": document_id,
                    "filename": filename,
                    "stage": self.state.stage.value,
                    "pages": self.state.page_count,
                    "chunks": 0,
                    "stop_after_ocr": True,
                }

            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.ocr_approved)

            self.state.stage = DocumentStage.TRANSLATION_PROCESSING
            await _mirror_state(workflow.info().workflow_id, "translation_processing", self.state.page_count, 0, None)

            translation_result = await workflow.execute_activity(
                detect_and_translate_pages_from_db,
                args=[workflow.info().workflow_id],
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=TRANSLATION_RETRY,
            )
            self.state.page_count = translation_result.get("page_count", self.state.page_count)
            self.state.translated_count = translation_result.get("translated_count", 0)
            self.state.translation_completed_at = _now_iso()
            self.state.stage = DocumentStage.TRANSLATION_REVIEW

            await _mirror_state(workflow.info().workflow_id, "translation_review", self.state.page_count, 0, None)

            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.translation_approved)

            self.state.stage = DocumentStage.CHUNKING
            await _mirror_state(workflow.info().workflow_id, "chunking", self.state.page_count, 0, None)

            chunk_result = await workflow.execute_activity(
                create_chunks_from_db,
                args=[workflow.info().workflow_id, chunk_size, chunk_overlap, min_tokens],
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=CHUNK_RETRY,
            )
            self.state.chunk_count = chunk_result.get("chunk_count", 0)
            self.state.chunks_completed_at = _now_iso()

            await workflow.execute_activity(
                auto_tag_chunks_from_db,
                args=[workflow.info().workflow_id, filename],
                start_to_close_timeout=timedelta(minutes=45),
                retry_policy=CHUNK_RETRY,
            )

            self.state.stage = DocumentStage.CHUNK_REVIEW

            await _mirror_state(workflow.info().workflow_id, "chunk_review", self.state.page_count, self.state.chunk_count, None)

            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.chunks_approved)

            self.state.stage = DocumentStage.READY_FOR_INGESTION
            await _mirror_state(workflow.info().workflow_id, "ready_for_ingestion", self.state.page_count, self.state.chunk_count, None)

            if not auto_approve:
                await workflow.wait_condition(lambda: self.state.ingestion_approved)

            self.state.stage = DocumentStage.INGESTING
            await _mirror_state(workflow.info().workflow_id, "ingesting", self.state.page_count, self.state.chunk_count, None)

            result = await workflow.execute_activity(
                ingest_document_from_db,
                args=[workflow.info().workflow_id, document_id, filename, marqo_url, index_name],
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=INGEST_RETRY,
            )

            self.state.ingested_at = _now_iso()
            self.state.stage = DocumentStage.COMPLETED

            await _mirror_state(workflow.info().workflow_id, "completed", self.state.page_count, self.state.chunk_count, None)

            workflow.logger.info(f"Pipeline complete for {filename}")
            return {
                "document_id": document_id,
                "filename": filename,
                "stage": self.state.stage.value,
                "pages": self.state.page_count,
                "chunks": self.state.chunk_count,
                "records_ingested": result.get("records_ingested", 0),
            }

        except Exception as e:
            self.state.stage = DocumentStage.FAILED
            self.state.error_message = str(e)
            workflow.logger.error(f"Pipeline failed: {e}")

            try:
                await _mirror_state(workflow.info().workflow_id, "failed", self.state.page_count, self.state.chunk_count, str(e))
            except Exception:
                pass
            raise

    @workflow.query
    def get_state(self) -> dict:
        if not self.state:
            return {}
        return {
            "document_id": self.state.document_id,
            "filename": self.state.filename,
            "filepath": self.state.filepath,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "page_count": self.state.page_count,
            "chunk_count": self.state.chunk_count,
            "translated_count": self.state.translated_count,
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

    @workflow.signal
    def approve_ocr(self):
        self.state.ocr_approved = True

    @workflow.signal
    def approve_translation(self):
        self.state.translation_approved = True

    @workflow.signal
    def approve_chunks(self):
        self.state.chunks_approved = True

    @workflow.signal
    def approve_ingestion(self):
        self.state.ingestion_approved = True


@dataclass
class ReingestionWorkflowState:
    document_id: str
    filename: str
    workflow_id: str
    stage: DocumentStage = DocumentStage.READY_FOR_INGESTION
    error_message: Optional[str] = None
    chunk_count: int = 0
    records_ingested: int = 0


@workflow.defn
class ReingestionWorkflow:
    """Lightweight workflow for re-ingesting completed documents to Marqo."""

    def __init__(self):
        self.state = None

    @workflow.run
    async def run(
        self,
        document_id: str,
        filename: str,
        original_workflow_id: str,
        page_count: int = 0,
        chunk_count: int = 0,
        marqo_url: str = "",
        index_name: str = "documents-index",
    ) -> dict:
        self.state = ReingestionWorkflowState(
            document_id=document_id,
            filename=filename,
            workflow_id=original_workflow_id,
            chunk_count=chunk_count,
        )

        try:
            await _mirror_state(original_workflow_id, "ingesting", page_count, chunk_count, None)

            self.state.stage = DocumentStage.INGESTING
            result = await workflow.execute_activity(
                ingest_document_from_db,
                args=[original_workflow_id, document_id, filename, marqo_url, index_name],
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=INGEST_RETRY,
            )

            self.state.records_ingested = result.get("records_ingested", 0)
            self.state.stage = DocumentStage.COMPLETED

            await _mirror_state(original_workflow_id, "completed", page_count, chunk_count, None)

            return {
                "document_id": document_id,
                "filename": filename,
                "stage": "completed",
                "chunks": chunk_count,
                "records_ingested": self.state.records_ingested,
            }

        except Exception as e:
            self.state.stage = DocumentStage.FAILED
            self.state.error_message = str(e)
            try:
                await _mirror_state(original_workflow_id, "failed", page_count, chunk_count, str(e))
            except Exception:
                pass
            raise

    @workflow.query
    def get_state(self) -> dict:
        if not self.state:
            return {}
        return {
            "document_id": self.state.document_id,
            "filename": self.state.filename,
            "workflow_id": self.state.workflow_id,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "chunk_count": self.state.chunk_count,
            "records_ingested": self.state.records_ingested,
        }


@dataclass
class TranslationOnlyWorkflowState:
    workflow_id: str
    document_id: str
    filename: str
    stage: DocumentStage = DocumentStage.OCR_REVIEW
    error_message: Optional[str] = None
    page_count: int = 0
    translated_count: int = 0
    translation_completed_at: Optional[str] = None


@workflow.defn
class TranslationOnlyWorkflow:
    """Resume from OCR review, run translation only, and stop at translation review."""

    def __init__(self):
        self.state = None

    @workflow.run
    async def run(
        self,
        original_workflow_id: str,
        document_id: str,
        filename: str,
    ) -> dict:
        self.state = TranslationOnlyWorkflowState(
            workflow_id=original_workflow_id,
            document_id=document_id,
            filename=filename,
        )

        try:
            await _mirror_state(original_workflow_id, "translation_processing", 0, 0, None)
            self.state.stage = DocumentStage.TRANSLATION_PROCESSING

            translation_result = await workflow.execute_activity(
                detect_and_translate_pages_from_db,
                args=[original_workflow_id],
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=TRANSLATION_RETRY,
            )
            self.state.page_count = translation_result.get("page_count", 0)
            self.state.translated_count = translation_result.get("translated_count", 0)
            self.state.translation_completed_at = _now_iso()
            self.state.stage = DocumentStage.TRANSLATION_REVIEW

            await _mirror_state(original_workflow_id, "translation_review", self.state.page_count, 0, None)

            return {
                "workflow_id": original_workflow_id,
                "document_id": document_id,
                "filename": filename,
                "stage": "translation_review",
                "page_count": self.state.page_count,
                "translated_count": self.state.translated_count,
            }
        except Exception as e:
            self.state.stage = DocumentStage.FAILED
            self.state.error_message = str(e)
            try:
                await _mirror_state(original_workflow_id, "failed", self.state.page_count, 0, str(e))
            except Exception:
                pass
            raise

    @workflow.query
    def get_state(self) -> dict:
        if not self.state:
            return {}
        return {
            "workflow_id": self.state.workflow_id,
            "document_id": self.state.document_id,
            "filename": self.state.filename,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "page_count": self.state.page_count,
            "translated_count": self.state.translated_count,
            "translation_completed_at": self.state.translation_completed_at,
        }


@dataclass
class OcrOnlyWorkflowState:
    workflow_id: str
    document_id: str
    filename: str
    stage: DocumentStage = DocumentStage.REGISTERED
    error_message: Optional[str] = None
    page_count: int = 0
    ocr_completed_at: Optional[str] = None


@workflow.defn
class OcrOnlyWorkflow:
    """Resume or retry OCR for an existing document and stop at OCR review."""

    def __init__(self):
        self.state = None

    @workflow.run
    async def run(
        self,
        original_workflow_id: str,
        document_id: str,
        filename: str,
        filepath: str,
    ) -> dict:
        self.state = OcrOnlyWorkflowState(
            workflow_id=original_workflow_id,
            document_id=document_id,
            filename=filename,
        )

        try:
            await _mirror_state(original_workflow_id, "ocr_processing", 0, 0, None)
            self.state.stage = DocumentStage.OCR_PROCESSING

            ocr_result = await workflow.execute_activity(
                run_ocr_and_store,
                args=[original_workflow_id, filepath],
                start_to_close_timeout=timedelta(minutes=90),
                retry_policy=OCR_RETRY,
            )
            self.state.page_count = ocr_result.get("page_count", 0)
            self.state.ocr_completed_at = _now_iso()
            self.state.stage = DocumentStage.OCR_REVIEW

            await _mirror_state(original_workflow_id, "ocr_review", self.state.page_count, 0, None)

            return {
                "workflow_id": original_workflow_id,
                "document_id": document_id,
                "filename": filename,
                "stage": "ocr_review",
                "page_count": self.state.page_count,
            }
        except Exception as e:
            self.state.stage = DocumentStage.FAILED
            self.state.error_message = str(e)
            try:
                await _mirror_state(original_workflow_id, "failed", self.state.page_count, 0, str(e))
            except Exception:
                pass
            raise

    @workflow.query
    def get_state(self) -> dict:
        if not self.state:
            return {}
        return {
            "workflow_id": self.state.workflow_id,
            "document_id": self.state.document_id,
            "filename": self.state.filename,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "page_count": self.state.page_count,
            "ocr_completed_at": self.state.ocr_completed_at,
        }


@dataclass
class ChunkingOnlyWorkflowState:
    workflow_id: str
    document_id: str
    filename: str
    stage: DocumentStage = DocumentStage.TRANSLATION_REVIEW
    error_message: Optional[str] = None
    page_count: int = 0
    chunk_count: int = 0
    chunks_completed_at: Optional[str] = None


@workflow.defn
class ChunkingOnlyWorkflow:
    """Resume or retry chunking for an existing document and stop at chunk review."""

    def __init__(self):
        self.state = None

    @workflow.run
    async def run(
        self,
        original_workflow_id: str,
        document_id: str,
        filename: str,
        page_count: int = 0,
        chunk_size: int = 450,
        chunk_overlap: int = 128,
        min_tokens: int = 100,
    ) -> dict:
        self.state = ChunkingOnlyWorkflowState(
            workflow_id=original_workflow_id,
            document_id=document_id,
            filename=filename,
            page_count=page_count,
        )

        try:
            await _mirror_state(original_workflow_id, "chunking", page_count, 0, None)
            self.state.stage = DocumentStage.CHUNKING

            chunk_result = await workflow.execute_activity(
                create_chunks_from_db,
                args=[original_workflow_id, chunk_size, chunk_overlap, min_tokens],
                start_to_close_timeout=timedelta(minutes=30),
                retry_policy=CHUNK_RETRY,
            )
            self.state.chunk_count = chunk_result.get("chunk_count", 0)
            self.state.chunks_completed_at = _now_iso()
            self.state.stage = DocumentStage.CHUNK_REVIEW

            await _mirror_state(original_workflow_id, "chunk_review", page_count, self.state.chunk_count, None)

            return {
                "workflow_id": original_workflow_id,
                "document_id": document_id,
                "filename": filename,
                "stage": "chunk_review",
                "chunk_count": self.state.chunk_count,
            }
        except Exception as e:
            self.state.stage = DocumentStage.FAILED
            self.state.error_message = str(e)
            try:
                await _mirror_state(original_workflow_id, "failed", page_count, self.state.chunk_count, str(e))
            except Exception:
                pass
            raise

    @workflow.query
    def get_state(self) -> dict:
        if not self.state:
            return {}
        return {
            "workflow_id": self.state.workflow_id,
            "document_id": self.state.document_id,
            "filename": self.state.filename,
            "stage": self.state.stage.value,
            "error_message": self.state.error_message,
            "page_count": self.state.page_count,
            "chunk_count": self.state.chunk_count,
            "chunks_completed_at": self.state.chunks_completed_at,
        }
