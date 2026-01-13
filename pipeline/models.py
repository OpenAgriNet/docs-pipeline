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
    OCR_REVIEW = "ocr_review"                    # Waiting for user to review/approve OCR
    TRANSLATION_PROCESSING = "translation_processing"  # Translating non-English content
    TRANSLATION_REVIEW = "translation_review"    # Waiting for user to review translations
    CHUNKING = "chunking"
    CHUNK_REVIEW = "chunk_review"                # Waiting for user to review/approve chunks
    READY_FOR_INGESTION = "ready_for_ingestion"  # Final review before ingestion
    INGESTING = "ingesting"
    COMPLETED = "completed"
    FAILED = "failed"


# Pipeline stage order for stepper UI
PIPELINE_STAGES = [
    ("registered", "Registered", "Document uploaded"),
    ("ocr_processing", "OCR", "Extracting text"),
    ("ocr_review", "OCR Review", "Review extracted text"),
    ("translation_processing", "Translation", "Translating content"),
    ("translation_review", "Translation Review", "Review translations"),
    ("chunking", "Chunking", "Creating chunks"),
    ("chunk_review", "Chunk Review", "Review chunks"),
    ("ready_for_ingestion", "Pre-Ingestion", "Final review"),
    ("ingesting", "Ingesting", "Uploading to vector DB"),
    ("completed", "Completed", "Processing complete"),
]


class PageData(BaseModel):
    """A page of OCR'd content."""
    page_number: int
    original_markdown: str
    edited_markdown: Optional[str] = None
    is_reviewed: bool = False
    reviewer_notes: Optional[str] = None

    # Translation fields
    detected_language: Optional[str] = None  # e.g., "hi", "gu", "en"
    translated_markdown: Optional[str] = None
    edited_translation: Optional[str] = None
    translation_reviewed: bool = False
    translation_notes: Optional[str] = None

    @property
    def markdown(self) -> str:
        """Get the best available markdown (edited > original)."""
        return self.edited_markdown if self.edited_markdown else self.original_markdown

    @property
    def final_text(self) -> str:
        """Get the final text for chunking (translated if available, else original)."""
        if self.edited_translation:
            return self.edited_translation
        if self.translated_markdown:
            return self.translated_markdown
        return self.markdown

    @property
    def needs_translation(self) -> bool:
        """Check if page needs translation (non-English detected)."""
        return self.detected_language and self.detected_language != "en"


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
    workflow_id: str  # The Temporal workflow ID (use this for API calls)
    filename: str
    stage: DocumentStage
    page_count: int
    chunk_count: int
    error_message: Optional[str] = None


# =============================================================================
# Audit Log Models
# =============================================================================

class AuditLogEntry(BaseModel):
    """A single audit log entry."""
    id: int
    workflow_id: str
    document_id: str
    action_type: str  # stage_change, page_edit, chunk_edit, approval, page_reset, chunk_reset
    entity_type: Optional[str] = None  # page, chunk, document
    entity_id: Optional[int] = None  # page_number or chunk_number
    field_name: Optional[str] = None
    old_value: Optional[str] = None  # JSON string
    new_value: Optional[str] = None  # JSON string
    metadata: Optional[str] = None  # JSON string
    timestamp: str


class AuditLogResponse(BaseModel):
    """Response for audit log listing."""
    logs: list[AuditLogEntry]
    total: int
    limit: int
    offset: int


# =============================================================================
# Settings Models
# =============================================================================

class SearchSettings(BaseModel):
    """Search configuration settings."""
    searchMethod: str = "HYBRID"  # TENSOR, LEXICAL, HYBRID
    limit: int = 10
    alpha: float = 0.7  # 0=lexical, 1=semantic
    rankingMethod: str = "rrf"  # rrf, normalize_linear
    showHighlights: bool = True
    efSearch: int = 256


class SearchSettingsUpdate(BaseModel):
    """Request to update search settings."""
    searchMethod: Optional[str] = None
    limit: Optional[int] = None
    alpha: Optional[float] = None
    rankingMethod: Optional[str] = None
    showHighlights: Optional[bool] = None
    efSearch: Optional[int] = None


class SettingEntry(BaseModel):
    """A single setting entry."""
    key: str
    value: str
    description: Optional[str] = None
    updated_at: str


class SettingsAuditResponse(BaseModel):
    """Response for settings audit log listing."""
    logs: list[AuditLogEntry]
    total: int
    limit: int
    offset: int
