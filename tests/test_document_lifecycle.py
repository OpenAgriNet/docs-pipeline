"""Document soft-delete + query enable/disable cascade to chunks."""


def test_soft_delete_excludes_all_chunks(db_connection):
    db = db_connection
    db.upsert_document(
        document_id="doc-life-1",
        workflow_id="wf-life-1",
        filename="life.pdf",
        filepath="/tmp/life.pdf",
        stage="completed",
        chunk_count=2,
    )
    db.save_chunks(
        "wf-life-1",
        [
            {"chunk_number": 1, "original_text": "one", "page_start": 1, "page_end": 1},
            {"chunk_number": 2, "original_text": "two", "page_start": 1, "page_end": 1},
        ],
    )

    db.set_document_disabled("wf-life-1", True)
    updated = db.set_all_chunks_excluded("wf-life-1", True)
    assert updated == 2

    chunks = db.get_chunks("wf-life-1", include_excluded=True)
    assert len(chunks) == 2
    assert all(bool(c.get("is_excluded")) for c in chunks)
    assert db.get_chunks("wf-life-1", include_excluded=False) == []


def test_query_disable_cascades_to_chunks(db_connection):
    db = db_connection
    db.upsert_document(
        document_id="doc-life-3",
        workflow_id="wf-life-3",
        filename="cascade.pdf",
        filepath="/tmp/cascade.pdf",
        stage="completed",
    )
    db.save_chunks(
        "wf-life-3",
        [
            {"chunk_number": 1, "original_text": "one", "page_start": 1, "page_end": 1},
            {"chunk_number": 2, "original_text": "two", "page_start": 1, "page_end": 1},
        ],
    )

    updated = db.set_document_query_enabled("wf-life-3", False)
    assert int(updated["query_enabled"]) == 0
    db.set_all_chunks_excluded("wf-life-3", True)
    chunks = db.get_chunks("wf-life-3", include_excluded=True)
    assert all(bool(c.get("is_excluded")) for c in chunks)

    db.set_document_query_enabled("wf-life-3", True)
    db.set_all_chunks_excluded("wf-life-3", False)
    chunks = db.get_chunks("wf-life-3", include_excluded=True)
    assert all(not bool(c.get("is_excluded")) for c in chunks)
    assert int(db.get_document("wf-life-3")["query_enabled"]) == 1


def test_enablement_flags_independent_of_disabled(db_connection):
    db = db_connection
    db.upsert_document(
        document_id="doc-life-2",
        workflow_id="wf-life-2",
        filename="env.pdf",
        filepath="/tmp/env.pdf",
        stage="completed",
    )
    db.set_document_enablement("wf-life-2", enabled_dev=False, enabled_prod=True)
    db.set_document_disabled("wf-life-2", True)
    row = db.get_document("wf-life-2")
    assert int(row["is_disabled"]) == 1
    assert int(row["enabled_dev"]) == 0
    assert int(row["enabled_prod"]) == 1


def test_hard_delete_chunk_removes_row_and_updates_count(db_connection):
    db = db_connection
    db.upsert_document(
        document_id="doc-chunk-del",
        workflow_id="wf-chunk-del",
        filename="chunk-del.pdf",
        filepath="/tmp/chunk-del.pdf",
        stage="completed",
        chunk_count=3,
    )
    db.save_chunks(
        "wf-chunk-del",
        [
            {"chunk_number": 1, "original_text": "one", "page_start": 1, "page_end": 1},
            {"chunk_number": 2, "original_text": "two", "page_start": 1, "page_end": 1},
            {"chunk_number": 3, "original_text": "three", "page_start": 2, "page_end": 2},
        ],
    )
    db.replace_chunk_tags(
        "wf-chunk-del",
        2,
        [{"dimension": "crop", "value": "wheat"}],
        source="manual",
    )

    assert db.delete_chunk("wf-chunk-del", 2) is True
    assert db.get_chunk("wf-chunk-del", 2) is None
    remaining = db.get_chunks("wf-chunk-del", include_excluded=True)
    assert [c["chunk_number"] for c in remaining] == [1, 3]
    assert int(db.get_document("wf-chunk-del")["chunk_count"]) == 2
    assert db.get_chunk_tags("wf-chunk-del", 2) == []
    assert db.delete_chunk("wf-chunk-del", 2) is False
