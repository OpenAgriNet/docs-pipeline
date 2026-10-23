"""Marqo provenance ingest fields and the /provenance/chunk resolver."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from pipeline.app import app
from pipeline.ingestion_records import get_marqo_record_id, prepare_ingestion_records
from tests.helpers import seed_document

pytestmark = pytest.mark.unit

DOC_ID = "abc123internaldocumentid00000001"
WF = "doc-ef226cde5062"
CHUNK = {
    "chunk_number": 3,
    "original_text": "## Deworming Schedule\n\nBody text",
    "edited_text": None,
    "token_count": 8,
    "page_start": 12,
    "page_end": 13,
    "is_excluded": False,
}


def _records(chunk, **kwargs):
    return prepare_ingestion_records(document_id=DOC_ID, filename="paripatra.pdf", chunks=[chunk], workflow_id=WF, **kwargs)


def test_prepare_records_includes_provenance_fields():
    records = _records(CHUNK, name_en="Sample Circular")
    assert len(records) == 1
    record = records[0]
    assert record["doc_id"] == DOC_ID
    assert record["workflow_id"] == WF
    assert record["filename"] == WF
    assert record["chunk_num"] == 3
    assert record["page_start"] == 12
    assert record["page_end"] == 13
    assert record["section"] == "Deworming Schedule"
    assert record["name_en"] == "Sample Circular"
    assert record["source"] == "docs-pipeline"
    assert record["type"] == "document"
    assert "text" in record
    assert record["is_reference"] is False


def test_get_marqo_record_id_is_stable_for_workflow_and_chunk():
    first = get_marqo_record_id(3, workflow_id=WF)
    assert first == get_marqo_record_id(3, workflow_id=WF)
    assert first != get_marqo_record_id(4, workflow_id=WF)


def test_prepare_records_marqo_id_ignores_text_edits():
    edited = {**CHUNK, "edited_text": "Corrected body after review"}
    assert _records(CHUNK)[0]["_id"] == _records(edited)[0]["_id"]
    assert _records(edited)[0]["text"] == "Corrected body after review"


def test_find_document_by_legacy_marqo_doc_id(db_connection):
    document_id, workflow_id = "legacy-doc-id-001", "doc-legacy000001"
    chunk = {"chunk_number": 5, "original_text": "Legacy chunk body", "token_count": 4, "page_start": 2, "page_end": 2}
    seed_document(db_connection, workflow_id, document_id=document_id, filename="legacy.pdf", stage="completed",
                  display_name="Legacy PDF", chunks=[chunk])
    legacy_marqo_doc_id = hashlib.md5(document_id.encode()).hexdigest()
    db_connection.upsert_document_index_status(
        workflow_id=workflow_id, index_name="documents-index", marqo_doc_id=legacy_marqo_doc_id,
        chunk_count_indexed=1, status="indexed",
    )

    doc = db_connection.find_document_by_doc_identifier(legacy_marqo_doc_id)
    assert doc is not None
    assert doc["workflow_id"] == workflow_id

    provenance = db_connection.resolve_chunk_provenance(doc_id=legacy_marqo_doc_id, chunk_num=5)
    assert provenance is not None
    assert provenance["workflow_id"] == workflow_id
    assert provenance["chunk_num"] == 5
    assert provenance["page_start"] == 2


def test_provenance_chunk_endpoint(db_connection):
    workflow_id = "doc-provenance01"
    chunk = {"chunk_number": 7, "original_text": "## Eligibility\n\nFarmers may apply.", "token_count": 6,
             "page_start": 4, "page_end": 5}
    seed_document(db_connection, workflow_id, document_id="prov-doc-id-xyz", filename="prov.pdf", stage="completed",
                  display_name="Provenance PDF", chunks=[chunk])

    response = TestClient(app).get("/provenance/chunk", params={"doc_id": workflow_id, "chunk_num": 7})
    assert response.status_code == 200
    payload = response.json()
    assert payload["workflow_id"] == workflow_id
    assert payload["doc_id"] == "prov-doc-id-xyz"
    assert payload["chunk_num"] == 7
    assert payload["page_start"] == 4
    assert payload["page_end"] == 5
    assert payload["section"] == "Eligibility"
    assert "/documents/doc-provenance01/pdf" in payload["pdf_url"]
    assert "tab=chunks&chunk=7" in payload["chunk_url"]
