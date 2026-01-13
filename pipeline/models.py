"""
Data models for the OCR pipeline.
These are used for Temporal workflow state and API responses.
"""

from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel


class DocumentStage(str, Enum):
    """Document processing stages."""
    REGISTERED = "registered"
    OCR_PROCESSING = "ocr_processing"
    OCR_REVIEW = "ocr_review"        # Waiting for user to review/approve OCR
    CHUNKING = "chunking"
    CHUNK_REVIEW = "chunk_review"    # Waiting for user to review/approve chunks
    READY_FOR_INGESTION = "ready_for_ingestion"
    INGESTING = "ingesting"
    COMPLETED = "completed"
    FAILED = "failed"


class PageData(BaseModel):
    """A page of OCR'd content."""
    page_number: int
    original_markdown: str
    edited_markdown: Optional[str] = None
    is_reviewed: bool = False
    reviewer_notes: Optional[str] = None

    @property
    def markdown(self) -> str:
        return self.edited_markdown if self.edited_markdown else self.original_markdown


class ChunkData(BaseModel):
    """A text chunk."""
    chunk_number: int
    original_text: str
    edited_text: Optional[str] = None
    token_count: int
    page_start: int = 1  # First page this chunk appears on
    page_end: int = 1    # Last page this chunk appears on
    is_reviewed: bool = False
    is_excluded: bool = False
    reviewer_notes: Optional[str] = None

    @property
    def text(self) -> str:
        return self.edited_text if self.edited_text else self.original_text

    @property
    def page_range(self) -> str:
        """Human-readable page range."""
        if self.page_start == self.page_end:
            return f"Page {self.page_start}"
        return f"Pages {self.page_start}-{self.page_end}"


class DocumentState(BaseModel):
    """Complete state of a document in the pipeline."""
    # Identity
    document_id: str
    filename: str
    filepath: str

    # Status
    stage: DocumentStage = DocumentStage.REGISTERED
    error_message: Optional[str] = None

    # Content
    pages: list[PageData] = []
    chunks: list[ChunkData] = []

    # Timestamps
    created_at: datetime = datetime.utcnow()
    ocr_completed_at: Optional[datetime] = None
    ocr_approved_at: Optional[datetime] = None
    chunking_completed_at: Optional[datetime] = None
    chunks_approved_at: Optional[datetime] = None
    ingested_at: Optional[datetime] = None


# =============================================================================
# API Request/Response Models
# =============================================================================

class RegisterRequest(BaseModel):
    filepath: str


class RegisterFolderRequest(BaseModel):
    directory: str


class PageUpdate(BaseModel):
    edited_markdown: Optional[str] = None
    is_reviewed: Optional[bool] = None
    reviewer_notes: Optional[str] = None


class ChunkUpdate(BaseModel):
    edited_text: Optional[str] = None
    is_reviewed: Optional[bool] = None
    is_excluded: Optional[bool] = None
    reviewer_notes: Optional[str] = None


class ApprovalRequest(BaseModel):
    approved: bool = True
    notes: Optional[str] = None


class DocumentSummary(BaseModel):
    document_id: str
    filename: str
    stage: DocumentStage
    page_count: int
    chunk_count: int
    error_message: Optional[str] = None
