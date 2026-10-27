"""GET /documents/{workflow_id}/marqo is a read of search availability.

It must not write SQLite, must page past a single Marqo search limit, and must
keep pipeline stage independent of whether hits are present.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import pipeline.db as db_mod
from pipeline.auth.models import local_bypass_user
from pipeline.routers import content as content_routes
from tests.helpers import seed_document

WF, DOC = "test-workflow-123", "test-doc-456"  # the conftest ``sample_document``


def _hit(n, doc_id=DOC, workflow_id=WF, text="x"):
    return {"_id": f"{doc_id}:{n}", "doc_id": doc_id, "workflow_id": workflow_id, "chunk_num": n, "text": text}


@pytest.fixture
def read_status(sample_document, fake_marqo):
    """Seed ``documents-index`` with ``records``, then GET the sample document's status."""
    def read(*records):
        index = fake_marqo.add_index("documents-index", records)
        status = content_routes.get_document_marqo_status(WF, local_bypass_user(), index_name="documents-index")
        return asyncio.run(status), index

    return read


def test_get_marqo_status_isolates_co_resident_documents(db_connection, read_status):
    """``document_id`` is a content fingerprint with no unique constraint, so two workflows can
    share it (same bytes, two uploads); one's status must not count the other's chunks (#73)."""
    seed_document(db_connection, "wf-co-resident", document_id=DOC, filename="same-bytes.pdf", stage="completed")
    result, index = read_status(
        *(_hit(n, text=f"{WF} chunk {n}") for n in (1, 2)),
        *(_hit(n, workflow_id="wf-co-resident", text=f"wf-co-resident chunk {n}") for n in (1, 2, 3)),
    )
    assert result["indexed_chunk_count"] == 2
    assert {hit["text"] for hit in result["hits"]} == {f"{WF} chunk 1", f"{WF} chunk 2"}
    assert f"workflow_id:{WF}" in index.filters[0]


def test_get_marqo_status_scopes_by_doc_id_across_documents(read_status):
    result, _ = read_status(_hit(1, text="mine"), _hit(1, doc_id="other-doc", workflow_id="other-workflow"))
    assert result["indexed_chunk_count"] == 1
    assert result["hits"][0]["doc_id"] == DOC


def test_get_marqo_status_does_not_write_index_status(db_connection, monkeypatch, read_status):
    db_connection.update_document_stage(WF, "ocr_processing")
    db_connection.upsert_document_index_status(
        WF, "documents-index", marqo_doc_id="legacy-hash", status="indexed",
        last_verified_at="2020-01-01T00:00:00", chunk_count_indexed=1,
    )
    spy = MagicMock(side_effect=AssertionError("GET /marqo must not write document_index_status"))
    monkeypatch.setattr(db_mod, "upsert_document_index_status", spy)

    result, index = read_status(_hit(1, doc_id="legacy-hash"))

    spy.assert_not_called()
    recorded = db_connection.get_document_index_status(WF, "documents-index")
    assert recorded["last_verified_at"] == "2020-01-01T00:00:00"
    assert recorded["status"] == "indexed"
    assert result["marqo_doc_id"] == "legacy-hash"
    assert result["pipeline_stage"] == "ocr_processing"
    assert result["search_available"] is True
    assert result["status"] == "indexed"
    assert "legacy-hash" in index.filters[0]


def test_get_marqo_status_keeps_stage_separate_when_missing(db_connection, read_status):
    db_connection.update_document_stage(WF, "chunk_review")
    result, _ = read_status()
    assert result["pipeline_stage"] == "chunk_review"
    assert result["search_available"] is False
    assert result["status"] == "missing"
    assert db_connection.get_document(WF)["stage"] == "chunk_review"


def test_get_marqo_status_pages_past_one_search_limit(read_status):
    result, index = read_status(*(_hit(n) for n in range(1, 1002)))
    assert result["indexed_chunk_count"] == 1001
    assert result["search_available"] is True
    assert [search["offset"] for search in index.searches] == [0, 1000]
