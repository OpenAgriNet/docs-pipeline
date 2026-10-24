"""Lock Temporal's persisted workflow/activity registration surface."""

from pipeline.temporal.client import TASK_QUEUE
from pipeline.temporal.registry import ACTIVITIES, WORKFLOWS


def test_temporal_registration_contract_is_stable():
    assert TASK_QUEUE == "ocr-pipeline"
    assert [workflow.__name__ for workflow in WORKFLOWS] == [
        "DocumentPipelineWorkflow",
        "ReingestionWorkflow",
        "TranslationOnlyWorkflow",
        "OcrOnlyWorkflow",
        "ChunkingOnlyWorkflow",
    ]
    assert [activity.__name__ for activity in ACTIVITIES] == [
        "run_ocr",
        "run_ocr_and_store",
        "create_chunks",
        "create_chunks_from_db",
        "auto_tag_chunks_from_db",
        "prepare_for_ingestion",
        "ingest_to_marqo",
        "ingest_document_from_db",
        "update_document_state",
        "detect_and_translate_pages",
        "detect_and_translate_pages_from_db",
        "persist_document_content",
    ]
