"""Tests for Marqo provenance ingest fields and /provenance/chunk resolver."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from pipeline.activities import _prepare_records, prepare_ingestion_records
from pipeline.api import app


@pytest.mark.unit
def test_prepare_records_includes_provenance_fields():
    chunks = [
        {
            "chunk_number": 3,
            "original_text": "## Deworming Schedule\n\nBody text",
            "edited_text": None,
            "token_count": 8,
            "page_start": 12,
            "page_end": 13,
            "is_excluded": False,
        }
    ]
    records = prepare_ingestion_records(
        document_id="abc123internaldocumentid00000001",
        filename="paripatra.pdf",
        chunks=chunks,
        workflow_id="doc-ef226cde5062",
        name_en="Sample Circular",
    )

    assert len(records) == 1
    record = records[0]
    assert record["doc_id"] == "abc123internaldocumentid00000001"
    assert record["workflow_id"] == "doc-ef226cde5062"
    assert record["filename"] == "doc-ef226cde5062"
    assert record["chunk_num"] == 3
    assert record["page_start"] == 12
    assert record["page_end"] == 13
    assert record["section"] == "Deworming Schedule"
    assert record["name_en"] == "Sample Circular"
    assert record["source"] == "docs-pipeline"
    assert record["type"] == "document"
    assert "text" in record
    assert record["is_reference"] is False


@pytest.mark.unit
def test_find_document_by_legacy_marqo_doc_id(db_connection):
    db = db_connection
    document_id = "legacy-doc-id-001"
    workflow_id = "doc-legacy000001"
    db.upsert_document(
        workflow_id=workflow_id,
        document_id=document_id,
        filename="legacy.pdf",
        filepath="/tmp/legacy.pdf",
        stage="completed",
        display_name="Legacy PDF",
    )
    db.save_chunks(
        workflow_id,
        [
            {
                "chunk_number": 5,
                "original_text": "Legacy chunk body",
                "token_count": 4,
                "page_start": 2,
                "page_end": 2,
            }
        ],
    )
    legacy_marqo_doc_id = hashlib.md5(document_id.encode()).hexdigest()
    db.upsert_document_index_status(
        workflow_id=workflow_id,
        index_name="documents-index",
        marqo_doc_id=legacy_marqo_doc_id,
        chunk_count_indexed=1,
        status="indexed",
    )

    doc = db.find_document_by_doc_identifier(legacy_marqo_doc_id)
    assert doc is not None
    assert doc["workflow_id"] == workflow_id

    provenance = db.resolve_chunk_provenance(doc_id=legacy_marqo_doc_id, chunk_num=5)
    assert provenance is not None
    assert provenance["workflow_id"] == workflow_id
    assert provenance["chunk_num"] == 5
    assert provenance["page_start"] == 2


@pytest.mark.unit
def test_provenance_chunk_endpoint(db_connection):
    db = db_connection
    workflow_id = "doc-provenance01"
    document_id = "prov-doc-id-xyz"
    db.upsert_document(
        workflow_id=workflow_id,
        document_id=document_id,
        filename="prov.pdf",
        filepath="/tmp/prov.pdf",
        stage="completed",
        display_name="Provenance PDF",
    )
    db.save_chunks(
        workflow_id,
        [
            {
                "chunk_number": 7,
                "original_text": "## Eligibility\n\nFarmers may apply.",
                "token_count": 6,
                "page_start": 4,
                "page_end": 5,
            }
        ],
    )

    client = TestClient(app)
    response = client.get(
        "/provenance/chunk",
        params={"doc_id": workflow_id, "chunk_num": 7},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["workflow_id"] == workflow_id
    assert payload["doc_id"] == document_id
    assert payload["chunk_num"] == 7
    assert payload["page_start"] == 4
    assert payload["page_end"] == 5
    assert payload["section"] == "Eligibility"
    assert "/documents/doc-provenance01/pdf" in payload["pdf_url"]
    assert "tab=chunks&chunk=7" in payload["chunk_url"]
