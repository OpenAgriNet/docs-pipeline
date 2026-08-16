"""Single registration manifest for the document Temporal worker."""

from .document_tasks import (
    auto_tag_chunks_from_db,
    create_chunks,
    create_chunks_from_db,
    detect_and_translate_pages,
    detect_and_translate_pages_from_db,
    ingest_document_from_db,
    ingest_to_marqo,
    persist_document_content,
    prepare_for_ingestion,
    run_ocr,
    run_ocr_and_store,
    update_document_state,
)
from .document_workflows import (
    ChunkingOnlyWorkflow,
    DocumentPipelineWorkflow,
    OcrOnlyWorkflow,
    ReingestionWorkflow,
    TranslationOnlyWorkflow,
)


WORKFLOWS = (
    DocumentPipelineWorkflow,
    ReingestionWorkflow,
    TranslationOnlyWorkflow,
    OcrOnlyWorkflow,
    ChunkingOnlyWorkflow,
)

ACTIVITIES = (
    run_ocr,
    run_ocr_and_store,
    create_chunks,
    create_chunks_from_db,
    auto_tag_chunks_from_db,
    prepare_for_ingestion,
    ingest_to_marqo,
    ingest_document_from_db,
    update_document_state,
    detect_and_translate_pages,
    detect_and_translate_pages_from_db,
    persist_document_content,
)
