"""GET /documents/{workflow_id}/marqo is a read of search availability.

It must not write SQLite, must page past a single Marqo search limit, and must
keep pipeline stage independent of whether hits are present.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

import pipeline.db as db_mod
import pipeline.vector_store as vector_store
from pipeline.auth.models import local_bypass_user
from pipeline.routers import content as content_routes


def _run(coro):
    return asyncio.run(coro)


class _StatusStore:
    def __init__(self, hits, fields=None):
        self.hits = list(hits)
        self.fields = fields or {"doc_id", "workflow_id", "filename", "text", "chunk_num"}
        self.searches: list[dict] = []

    def field_names(self, _index):
        return set(self.fields)

    def get_settings(self, _index):
        return {"allFields": [{"name": name, "type": "text"} for name in sorted(self.fields)]}

    def search(
        self,
        index,
        q="",
        filter_string="",
        limit=10,
        offset=0,
        attributes_to_retrieve=None,
    ):
        self.searches.append(
            {
                "index": index,
                "filter_string": filter_string,
                "limit": limit,
                "offset": offset,
            }
        )
        page = self.hits[offset : offset + limit]
        return {"hits": page}


@pytest.mark.api
def test_get_marqo_status_does_not_write_index_status(db_connection, monkeypatch, sample_document):
    db_connection.update_document_stage(sample_document["workflow_id"], "ocr_processing")
    db_connection.upsert_document_index_status(
        sample_document["workflow_id"],
        "documents-index",
        marqo_doc_id="legacy-hash",
        status="indexed",
        last_verified_at="2020-01-01T00:00:00",
        chunk_count_indexed=1,
    )
    spy = MagicMock(side_effect=AssertionError("GET /marqo must not write document_index_status"))
    monkeypatch.setattr(db_mod, "upsert_document_index_status", spy)

    store = _StatusStore(
        [
            {
                "_id": "legacy-hash:1",
                "doc_id": "legacy-hash",
                "chunk_num": 1,
                "filename": "test.pdf",
                "text": "chunk",
            }
        ]
    )
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: store)

    result = _run(
        content_routes.get_document_marqo_status(
            sample_document["workflow_id"],
            local_bypass_user(),
            index_name="documents-index",
        )
    )

    spy.assert_not_called()
    recorded = db_connection.get_document_index_status(
        sample_document["workflow_id"], "documents-index"
    )
    assert recorded["last_verified_at"] == "2020-01-01T00:00:00"
    assert recorded["status"] == "indexed"
    assert result["marqo_doc_id"] == "legacy-hash"
    assert result["pipeline_stage"] == "ocr_processing"
    assert result["search_available"] is True
    assert result["status"] == "indexed"
    assert "legacy-hash" in store.searches[0]["filter_string"]


@pytest.mark.api
def test_get_marqo_status_keeps_stage_separate_when_missing(db_connection, monkeypatch, sample_document):
    db_connection.update_document_stage(sample_document["workflow_id"], "chunk_review")
    store = _StatusStore([])
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: store)

    result = _run(
        content_routes.get_document_marqo_status(
            sample_document["workflow_id"],
            local_bypass_user(),
            index_name="documents-index",
        )
    )

    assert result["pipeline_stage"] == "chunk_review"
    assert result["search_available"] is False
    assert result["status"] == "missing"
    refreshed = db_connection.get_document(sample_document["workflow_id"])
    assert refreshed["stage"] == "chunk_review"


@pytest.mark.api
def test_get_marqo_status_pages_past_one_search_limit(db_connection, monkeypatch, sample_document):
    hits = [
        {
            "_id": f"{sample_document['document_id']}:{n}",
            "doc_id": sample_document["document_id"],
            "chunk_num": n,
            "filename": "test.pdf",
            "text": "x",
        }
        for n in range(1, 1002)
    ]
    store = _StatusStore(hits)
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: store)

    result = _run(
        content_routes.get_document_marqo_status(
            sample_document["workflow_id"],
            local_bypass_user(),
            index_name="documents-index",
        )
    )

    assert result["indexed_chunk_count"] == 1001
    assert result["search_available"] is True
    assert [call["offset"] for call in store.searches] == [0, 1000]
