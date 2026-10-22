"""Tests for indexing one webhook advisory record into a vector store."""

import copy

import pytest

from pipeline.advisory_index import index_advisory_record
from pipeline.advisory_chunks import build_chunks
from pipeline.vector_store.qdrant_store import _record_payload
from tests.test_advisory_chunks import SORGHUM_PAYLOAD, make_row


class FakeStore:
    def __init__(self, delete_result=None, fail_upsert=False):
        self.calls = []
        self.records = []
        self._delete_result = delete_result or {"deleted": -1}
        self._fail_upsert = fail_upsert

    def delete_by_doc_id(self, name, doc_id):
        self.calls.append(("delete", name, doc_id))
        return self._delete_result

    def upsert(self, name, records, batch_size=32):
        self.calls.append(("upsert", name, len(records)))
        if self._fail_upsert:
            raise RuntimeError("qdrant down")
        self.records = list(records)
        return {"records_ingested": len(records)}


def _store_row(db, payload):
    db.upsert_webhook_records("webhook", [payload])
    with db.get_connection() as conn:
        return conn.execute(
            "SELECT id FROM webhook_records WHERE external_id = ?", (str(payload["contentID"]),)
        ).fetchone()["id"]


def _count(_text):
    return 250


@pytest.mark.unit
@pytest.mark.db
class TestIndexAdvisoryRecord:
    def test_published_record_is_deleted_then_upserted_then_marked(self, db_connection):
        db = db_connection
        rid = _store_row(db, copy.deepcopy(SORGHUM_PAYLOAD))
        store = FakeStore()
        result = index_advisory_record(rid, store, "document-data", _count)
        assert [c[0] for c in store.calls] == ["delete", "upsert"]
        assert store.calls[0][1:] == ("document-data", rid)
        assert result == {"record_id": rid, "status": "indexed", "chunks": 1}
        assert db.get_unindexed_webhook_record_ids() == []

    def test_unpublished_record_only_deletes(self, db_connection):
        db = db_connection
        payload = copy.deepcopy(SORGHUM_PAYLOAD)
        payload["isPublished"] = 0
        rid = _store_row(db, payload)
        store = FakeStore()
        result = index_advisory_record(rid, store, "document-data", _count)
        assert [c[0] for c in store.calls] == ["delete"]
        assert result["status"] == "deleted" and result["chunks"] == 0
        assert db.get_unindexed_webhook_record_ids() == []

    def test_unpublished_record_also_deletes_from_the_other_collection(self, db_connection):
        db = db_connection
        payload = copy.deepcopy(SORGHUM_PAYLOAD)
        payload["isPublished"] = 0
        rid = _store_row(db, payload)
        dev, prod = FakeStore(), FakeStore()
        index_advisory_record(
            rid, dev, "document-data", _count, also_delete_from=(prod, "prod-collection")
        )
        assert prod.calls == [("delete", "prod-collection", rid)]

    def test_published_record_does_not_touch_the_other_collection(self, db_connection):
        db = db_connection
        rid = _store_row(db, copy.deepcopy(SORGHUM_PAYLOAD))
        dev, prod = FakeStore(), FakeStore()
        index_advisory_record(
            rid, dev, "document-data", _count, also_delete_from=(prod, "prod-collection")
        )
        assert prod.calls == []

    def test_missing_record_does_nothing(self, db_connection):
        store = FakeStore()
        result = index_advisory_record("nope", store, "document-data", _count)
        assert store.calls == []
        assert result == {"record_id": "nope", "status": "missing", "chunks": 0}

    def test_record_without_poplist_is_marked_indexed(self, db_connection):
        db = db_connection
        payload = copy.deepcopy(SORGHUM_PAYLOAD)
        payload["popList"] = []
        rid = _store_row(db, payload)
        store = FakeStore()
        result = index_advisory_record(rid, store, "document-data", _count)
        assert [c[0] for c in store.calls] == ["delete"]
        assert result["status"] == "deleted"
        assert db.get_unindexed_webhook_record_ids() == []

    def test_upsert_failure_leaves_record_unindexed(self, db_connection):
        db = db_connection
        rid = _store_row(db, copy.deepcopy(SORGHUM_PAYLOAD))
        with pytest.raises(RuntimeError, match="qdrant down"):
            index_advisory_record(rid, FakeStore(fail_upsert=True), "document-data", _count)
        assert db.get_unindexed_webhook_record_ids() == [rid]

    def test_delete_error_other_than_missing_collection_fails(self, db_connection):
        db = db_connection
        rid = _store_row(db, copy.deepcopy(SORGHUM_PAYLOAD))
        store = FakeStore(delete_result={"deleted": 0, "error": "connection refused"})
        with pytest.raises(RuntimeError, match="connection refused"):
            index_advisory_record(rid, store, "document-data", _count)
        assert db.get_unindexed_webhook_record_ids() == [rid]

    def test_missing_collection_on_delete_is_not_an_error(self, db_connection):
        db = db_connection
        rid = _store_row(db, copy.deepcopy(SORGHUM_PAYLOAD))
        store = FakeStore(delete_result={"deleted": 0, "error": "Collection `x` doesn't exist!"})
        result = index_advisory_record(rid, store, "x", _count)
        assert result["status"] == "indexed"

    def test_promote_marks_promoted_not_indexed(self, db_connection):
        db = db_connection
        rid = _store_row(db, copy.deepcopy(SORGHUM_PAYLOAD))
        index_advisory_record(rid, FakeStore(), "prod", _count, mark="promoted")
        assert db.get_unindexed_webhook_record_ids() == [rid]
        assert db.advisory_status_counts()["promoted"] == 1

    def test_reposting_unchanged_record_reuses_point_ids(self, db_connection):
        db = db_connection
        rid = _store_row(db, copy.deepcopy(SORGHUM_PAYLOAD))
        first, second = FakeStore(), FakeStore()
        index_advisory_record(rid, first, "document-data", _count)
        index_advisory_record(rid, second, "document-data", _count)
        assert [r["_id"] for r in first.records] == [r["_id"] for r in second.records]


def test_payload_whitelist_keeps_advisory_fields():
    chunk = build_chunks(make_row(), _count)[0]
    payload = _record_payload(chunk)
    for key in (
        "document_kind", "external_id", "pop_id", "slug", "crop", "domain", "subdomain",
        "institute", "creator", "problem_type", "problem", "season", "start_period",
        "end_period", "updated_at", "section", "chunk_id", "text", "doc_id", "title_en",
    ):
        assert key in payload, key
    assert payload["document_kind"] == "advisory"
