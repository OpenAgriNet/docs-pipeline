"""Temporal activities: MinIO keys, OCR guardrails, record prep, Marqo ingest
(index lifecycle, scoped replace, failure accounting) and progress heartbeats."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

import pytest

import pipeline.temporal.document_tasks as activities
from pipeline import vector_store
from pipeline.ingestion_records import get_marqo_record_id, prepare_ingestion_records
from tests.helpers import PASSAGE_FIELDS, seed_document

INDEX = "documents-index"


def _pages(*texts):
    return [{"page_number": n, "original_markdown": text} for n, text in enumerate(texts, start=1)]


# --- MinIO: artifact keys are prefixed by tenant (§5.3) ----------------------------


def test_object_name_prefixes_instance():
    key = activities._minio_object_name("tenant-a", "wf-123", "original_upload", "My File.pdf")
    assert key == "tenant-a/wf-123/original_upload/My_File.pdf"


def test_object_name_normalizes_and_defaults_instance():
    assert activities._minio_object_name("Tenant-A", "wf", "t", "f.json").startswith("tenant-a/")
    # No instance falls back to the default tenant, never an un-prefixed key.
    default_key = activities._minio_object_name(None, "wf", "t", "f.json")
    assert default_key.count("/") == 3
    assert not default_key.startswith("/")
    assert default_key.endswith("wf/t/f.json")


def test_get_minio_client_requires_credentials(monkeypatch):
    monkeypatch.delenv("MINIO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("MINIO_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="required"):
        activities.get_minio_client()


# --- OCR guardrails ----------------------------------------------------------------


def test_validate_ocr_pages_raises_on_empty_pdf_output(temp_pdf_file):
    with pytest.raises(RuntimeError, match="OCR produced 0 pages"):
        activities._validate_ocr_pages_for_pdf(str(temp_pdf_file), [], filename="demo.pdf")


def test_validate_ocr_pages_raises_on_count_mismatch(temp_pdf_file, monkeypatch):
    monkeypatch.setattr(activities, "_pdf_page_count", lambda _path: 2)
    with pytest.raises(RuntimeError, match="page count mismatch"):
        activities._validate_ocr_pages_for_pdf(str(temp_pdf_file), _pages("ok"), filename="demo.pdf")


def test_validate_ocr_pages_allows_matching_output(temp_pdf_file):
    activities._validate_ocr_pages_for_pdf(str(temp_pdf_file), _pages("ok"), filename="demo.pdf")


def test_drop_degenerate_pages_fails_when_all_bad():
    with pytest.raises(RuntimeError, match="only degenerate repetition"):
        activities._drop_degenerate_ocr_pages(_pages("o" * 600), filename="bad.pdf")


def test_drop_degenerate_pages_keeps_good_pages():
    kept, dropped = activities._drop_degenerate_ocr_pages(
        _pages("o" * 600, "Real veterinary guidance text."), filename="mixed.pdf"
    )
    assert [p["page_number"] for p in kept] == [2]
    assert dropped == [1]


def test_save_pages_does_not_delete_omitted_rows(db_connection):
    seed_document(db_connection, "wf", pages=["page one", "page two"])
    db_connection.save_pages("wf", [{"page_number": 2, "original_markdown": "page two"}])
    assert sorted(p["page_number"] for p in db_connection.get_pages("wf")) == [1, 2]


def test_finalize_ocr_pages_removes_degenerate_rows(db_connection):
    seed_document(db_connection, "wf", pages=["o" * 600, "Real veterinary guidance text."])
    kept = activities._finalize_ocr_pages("wf", db_connection.get_pages("wf"), filename="mixed.pdf")
    assert [p["page_number"] for p in kept] == [2]
    stored = db_connection.get_pages("wf")
    assert [(p["page_number"], p["original_markdown"]) for p in stored] == [(2, "Real veterinary guidance text.")]


def test_finalize_ocr_pages_persists_in_memory_pages(db_connection):
    """CSV/XLSX pages are not persisted before finalization."""
    seed_document(db_connection, "wf")
    kept = activities._finalize_ocr_pages(
        "wf", _pages("Valid spreadsheet data.", "More spreadsheet data."), filename="data.xlsx"
    )
    assert [p["page_number"] for p in kept] == [1, 2]
    stored = db_connection.get_pages("wf")
    assert [p["page_number"] for p in stored] == [1, 2]
    assert stored[0]["original_markdown"] == "Valid spreadsheet data."


# --- chunk and record preparation ----------------------------------------------------


def test_create_chunks_uses_edited_markdown(db_connection, minio_uploads):
    seed_document(db_connection, "wf", pages=[{
        "page_number": 1, "original_markdown": "Original content that should not appear.",
        "edited_markdown": "Edited content that should appear. " * 30, "detected_language": "en",
    }])
    asyncio.run(activities.create_chunks_from_db("wf", chunk_size=50, chunk_overlap=10, min_tokens=5))
    chunks = db_connection.get_chunks("wf", include_excluded=True)
    assert len(chunks) > 0
    all_text = " ".join(c["original_text"] for c in chunks)
    assert "Edited content" in all_text
    assert "Original content that should not appear" not in all_text


def _chunk(number, text, **fields):
    return {
        "chunk_number": number, "original_text": text, "edited_text": None,
        "source_pages": [1], "token_count": 5, "is_excluded": False, **fields,
    }


def test_prepare_records_basic():
    chunks = [_chunk(1, "Test chunk one"), _chunk(2, "Test chunk two", source_pages=[1, 2])]
    records = prepare_ingestion_records(document_id="test-doc", filename="test.pdf", chunks=chunks)
    assert len(records) == 2
    for record in records:
        assert {"_id", "doc_id", "text", "chunk_num"} <= record.keys()
        assert record["doc_id"] == "test-doc"


@pytest.mark.parametrize("chunks, texts", [
    ([_chunk(1, "Included"), _chunk(2, "Excluded", is_excluded=True)], ["Included"]),
    ([_chunk(1, "Original", edited_text="Edited")], ["Edited"]),
], ids=["excluded-chunks-dropped", "edited-text-preferred"])
def test_prepare_records_text_selection(chunks, texts):
    records = prepare_ingestion_records(document_id="test-doc", filename="test.pdf", chunks=chunks)
    assert [r["text"] for r in records] == texts


def test_update_state(db_connection):
    seed_document(db_connection, "wf-state")
    asyncio.run(activities.update_document_state(
        workflow_id="wf-state", stage="ocr_processing", page_count=5, chunk_count=0
    ))
    doc = db_connection.get_document("wf-state")
    assert doc["stage"] == "ocr_processing"
    assert doc["page_count"] == 5


def _ingest(workflow_id, document_id=None, **kwargs):
    return asyncio.run(activities.ingest_document_from_db(
        workflow_id=workflow_id, document_id=document_id or f"doc-{workflow_id}",
        filename="doc.pdf", index_name=INDEX, **kwargs,
    ))


def _ready_to_ingest(db, fake_marqo, workflow_id, *, owner=None, **index_kwargs):
    """Four chunks of ``workflow_id`` to ingest into an index already holding one
    earlier record of ``owner`` (default: the same workflow) under its doc_id."""
    seed_document(db, workflow_id, stage="ready_for_ingestion", chunks=[f"chunk {n}" for n in range(1, 5)])
    return fake_marqo.add_index(INDEX, [{
        "_id": "old-1", "doc_id": f"doc-{workflow_id}", "workflow_id": owner or workflow_id, "chunk_num": 1,
        "text": "old stale chunk", "text_for_embedding": "passage: old stale chunk",
    }], **index_kwargs)


# --- an existing index is never deleted or recreated implicitly ---------------------
# A live legacy index can predate the passage schema (no `section`) while holding the
# whole production corpus. The old code took that, or a transient schema read, as a
# "confirmed mismatch" and ran delete_index + create_index.


def test_verification_error_does_not_recreate_index(db_connection, fake_marqo, minio_uploads):
    """A transient schema read propagates for Temporal to retry."""
    index = _ready_to_ingest(db_connection, fake_marqo, "wf")
    index.fail("get_settings", RuntimeError("transient marqo blip"))
    before = list(index.records)
    with pytest.raises(RuntimeError, match="transient"):
        _ingest("wf")
    assert (fake_marqo.deleted_indexes, fake_marqo.created_indexes) == ([], [])
    assert index.records == before


def test_legacy_index_missing_core_fields_is_not_deleted(db_connection, fake_marqo, minio_uploads):
    index = _ready_to_ingest(db_connection, fake_marqo, "wf", fields=PASSAGE_FIELDS - {"section"})
    _ingest("wf")
    assert fake_marqo.deleted_indexes == [], "existing index must never be deleted implicitly"
    assert fake_marqo.created_indexes == [], "existing index must never be recreated implicitly"
    assert len(index.records) == 4, "ingest proceeds with the fields the index accepts"
    assert not any("section" in record for record in index.records)


def test_missing_index_is_still_provisioned(db_connection, fake_marqo, minio_uploads):
    seed_document(db_connection, "wf", stage="ready_for_ingestion", chunks=["chunk 1"])
    _ingest("wf")
    assert fake_marqo.created_indexes == [INDEX]
    assert fake_marqo.deleted_indexes == []
    assert len(fake_marqo.indexes[INDEX].records) == 1


def test_reingest_replaces_only_this_workflows_records(db_connection, fake_marqo, minio_uploads):
    """This run's stale-id record is replaced, not appended to; a document sharing its doc_id is untouched."""
    seed_document(
        db_connection, "wf-first", document_id="shared-doc", stage="ready_for_ingestion",
        chunks=[{
            "chunk_number": 1, "original_text": "Old searchable text", "edited_text": "New corrected text",
            "token_count": 4, "page_start": 1, "page_end": 1,
        }],
    )
    stale_id = hashlib.md5(f"{hashlib.md5(b'shared-doc').hexdigest()}_1_Old searchable text".encode()).hexdigest()
    co_resident = prepare_ingestion_records(
        "shared-doc", "b.pdf", [{"chunk_number": 1, "original_text": "second one"}], workflow_id="wf-second"
    )
    index = fake_marqo.add_index(INDEX, [{
        "_id": stale_id, "doc_id": "shared-doc", "workflow_id": "wf-first", "chunk_num": 1,
        "text": "Old searchable text", "text_for_embedding": "passage: Old searchable text",
    }, *co_resident])

    _ingest("wf-first", document_id="shared-doc")

    by_workflow = {record["workflow_id"]: record for record in index.records}
    assert len(index.records) == 2
    assert stale_id not in {record["_id"] for record in index.records}
    assert by_workflow["wf-first"]["_id"] == get_marqo_record_id(1, workflow_id="wf-first")
    assert by_workflow["wf-first"]["text"] == "New corrected text"
    assert by_workflow["wf-second"] == co_resident[0], "the purge reached a co-resident document"
    assert [u["artifact_type"] for u in minio_uploads] == ["marqo_payload_export"]


@pytest.mark.parametrize("owner, index_kwargs, match", [
    ("wf-other", {"fields": PASSAGE_FIELDS - {"workflow_id"}}, "does not expose workflow_id"),
    (None, {"tensor_fields": ()}, "declares no tensor fields"),
], ids=["no-workflow-id-field", "no-tensor-field"])
def test_ingest_refuses_an_unsafe_index_before_purging(
    db_connection, fake_marqo, minio_uploads, owner, index_kwargs, match
):
    index = _ready_to_ingest(db_connection, fake_marqo, "wf-refused", owner=owner, **index_kwargs)
    before = list(index.records)
    with pytest.raises(RuntimeError, match=match):
        _ingest("wf-refused")
    assert index.records == before


class _RaisingPurgeStore:
    """A store whose purge raises. ``MarqoStore.delete_document`` catches every
    exception itself, so this cannot be made through the client fake."""

    url = "http://marqo.test"

    def describe_index(self, index):
        return vector_store.IndexSchemaReport(
            exists=True, field_names=set(PASSAGE_FIELDS), tensor_fields={"text_for_embedding"}, missing_core=[],
        )

    def delete_document(self, document_id, index, workflow_id=None):
        raise RuntimeError("adapter exploded")


def _reject_chunk_4(index, _monkeypatch):
    index.rejected[get_marqo_record_id(4, workflow_id="wf-fail")] = {
        "status": 500, "error": "forced failure", "code": "ERR_FORCED",
    }


@pytest.mark.parametrize("fault, match, indexed, phase", [
    pytest.param(_reject_chunk_4, "add_documents failed", 3, "add_documents", id="batch-2-item-rejected"),
    pytest.param(
        lambda index, _: index.fail("add_documents", RuntimeError("forced request failure"), on_call=2),
        "request failed", 2, "add_documents", id="batch-2-request-raises",
    ),
    pytest.param(
        lambda index, _: index.fail("delete_documents", RuntimeError("forced delete failure")),
        "purge before ingest failed", 0, "purge", id="purge-delete-fails",
    ),
    # The phase comes from where the failure happened, not from the message text.
    pytest.param(
        lambda _, monkeypatch: monkeypatch.setattr(activities, "get_vector_store", _RaisingPurgeStore),
        "purge before ingest failed", 0, "purge", id="purge-adapter-raises",
    ),
    pytest.param(
        lambda index, _: index.fail("get_stats", RuntimeError("forced stats failure")),
        "stats lookup failed", 4, "post_ingest_stats", id="stats-fail-after-all-batches",
    ),
])
def test_failed_ingest_records_index_failed_with_true_partial_count(
    db_connection, fake_marqo, minio_uploads, monkeypatch, fault, match, indexed, phase
):
    fault(_ready_to_ingest(db_connection, fake_marqo, "wf-fail"), monkeypatch)
    with pytest.raises(RuntimeError, match=match):
        _ingest("wf-fail", batch_size=2)
    status = db_connection.get_document_index_status("wf-fail", INDEX)
    assert status["status"] == "index_failed"
    assert status["chunk_count_indexed"] == indexed
    assert f'"phase": "{phase}"' in status["details_json"]


# --- progress heartbeats: translation, auto-tag, ingest ------------------------------


@pytest.fixture
def heartbeats(monkeypatch):
    beats: list[dict] = []
    monkeypatch.setattr(activities.activity, "heartbeat", lambda payload=None, **_kwargs: beats.append(payload or {}))
    return beats


def _translator(monkeypatch, step):
    """Swap the translator, behind its real signature, for ``step(pages, progress_callback)``."""

    async def fake(pages, *, target_language="en", config=None, log=None, force_retranslate=False,
                   progress_callback=None):
        return step(pages, progress_callback)

    monkeypatch.setattr(activities, "translate_pages", fake)


def test_translation_progress_persists_page_before_failure(db_connection, minio_uploads, monkeypatch):
    seed_document(
        db_connection, "wf-t", stage="translation_processing",
        pages=["Gujarati page one text", "Gujarati page two text"],
    )

    def crash_after_page_one(pages, progress_callback):
        pages[0].update(
            translated_markdown="translated one", translation_provider="gemma_vllm", translation_model="gemma-4",
            translation_target_language="en", translated_at="2026-08-25T00:00:00",
        )
        progress_callback({
            "phase": "translation", "pages_total": 2, "pages_completed": 1,
            "translated_count": 1, "failed_count": 0, "translated_page": pages[0],
        })
        raise RuntimeError("forced failure after first translated page")

    _translator(monkeypatch, crash_after_page_one)
    with pytest.raises(RuntimeError, match="forced failure"):
        asyncio.run(activities.detect_and_translate_pages_from_db("wf-t"))
    by_number = {int(p["page_number"]): p for p in db_connection.get_pages("wf-t")}
    assert by_number[1]["translated_markdown"] == "translated one"
    assert by_number[2]["translated_markdown"] is None

    seen: list = []

    def retry(pages, _progress_callback):
        seen.extend(p.get("translated_markdown") for p in pages)
        return pages

    _translator(monkeypatch, retry)
    asyncio.run(activities.detect_and_translate_pages_from_db("wf-t"))
    assert seen == ["translated one", None], "the retry must start from the persisted page"


def test_translation_callback_keeps_event_remaining_work_total(
    db_connection, minio_uploads, heartbeats, monkeypatch
):
    seed_document(
        db_connection, "wf-t", stage="translation_processing", page_count=4,
        pages=["page one", "page two", "page three", "page four"],
    )

    def step(pages, progress_callback):
        progress_callback({"phase": "detection", "pages_total": 4, "pages_completed": 4})
        progress_callback({
            "phase": "translation", "pages_total": 2, "pages_completed": 1, "translated_count": 1, "failed_count": 0,
        })
        return pages

    _translator(monkeypatch, step)
    asyncio.run(activities.detect_and_translate_pages_from_db("wf-t"))
    translation_ticks = [event for event in heartbeats if event.get("phase") == "translation"]
    detection_ticks = [event for event in translation_ticks if event.get("pages_total") == 4]
    assert detection_ticks, "detection must still heartbeat for liveness"
    assert detection_ticks[0]["pages_completed"] == 0
    assert translation_ticks[-1]["pages_completed"] == 1
    assert translation_ticks[-1]["pages_total"] == 2


def test_translation_callback_missing_total_falls_back_to_page_count(
    db_connection, minio_uploads, heartbeats, monkeypatch
):
    seed_document(
        db_connection, "wf-t", stage="translation_processing", page_count=3,
        pages=["page one", "page two", "page three"],
    )

    def step(pages, progress_callback):
        progress_callback({"phase": "translation", "pages_completed": 2})
        return pages

    _translator(monkeypatch, step)
    asyncio.run(activities.detect_and_translate_pages_from_db("wf-t"))
    translation_ticks = [event for event in heartbeats if event.get("phase") == "translation"]
    assert translation_ticks[-1]["pages_total"] == 3
    assert translation_ticks[-1]["pages_completed"] == 2


def test_detection_heartbeat_uses_already_translated_sqlite_count(
    db_connection, minio_uploads, heartbeats, monkeypatch
):
    seed_document(
        db_connection, "wf-t", stage="translation_processing", page_count=3,
        pages=[{"page_number": 1, "original_markdown": "page one", "translated_markdown": "one"},
               "page two", "page three"],
    )

    def step(pages, progress_callback):
        progress_callback({"phase": "detection", "pages_total": 3, "pages_completed": 3})
        return pages

    _translator(monkeypatch, step)
    asyncio.run(activities.detect_and_translate_pages_from_db("wf-t"))
    detection_ticks = [e for e in heartbeats if e.get("phase") == "translation" and e.get("pages_total") == 3]
    assert detection_ticks
    assert detection_ticks[0]["pages_completed"] == 1


def test_auto_tag_emits_heartbeat_progress(db_connection, heartbeats, monkeypatch):
    import pipeline.domain_tags.base as tag_base
    import pipeline.domain_tags.gemma_tagger as gemma_tagger
    import pipeline.domain_tags.service as tag_service
    from pipeline.domain_tags.base import DomainTag

    seed_document(db_connection, "wf-tag", stage="chunking", chunks=["chunk one", "chunk two"])
    monkeypatch.setattr(
        tag_service, "load_domain_tagging_config", lambda: SimpleNamespace(enabled=True, strict_taxonomy=False)
    )
    monkeypatch.setattr(tag_service, "get_domain_tagger", lambda _cfg: object())
    monkeypatch.setattr(tag_base, "load_taxonomy_for_instance", lambda _instance: {})

    async def fake_auto_tag_chunks(chunks, *, filename="", doc_context="", tagger=None, taxonomy=None, log=None,
                                   progress_callback=None):
        for i, chunk in enumerate(chunks, 1):
            progress_callback({
                "chunks_total": len(chunks), "chunks_completed": i, "chunk_number": chunk["chunk_number"],
            })
        return {1: [DomainTag(dimension="domain", value="general", source="auto")], 2: []}

    monkeypatch.setattr(gemma_tagger, "auto_tag_chunks", fake_auto_tag_chunks)

    result = asyncio.run(activities.auto_tag_chunks_from_db("wf-tag", filename="doc.pdf"))
    assert result["tagged_chunks"] == 1
    auto_tag_events = [h for h in heartbeats if h.get("stage") == "auto_tag"]
    assert any(h.get("chunks_completed") == 0 for h in auto_tag_events)
    assert any(h.get("chunks_completed") == 2 for h in auto_tag_events)


def test_ingest_emits_heartbeat_per_batch(db_connection, fake_marqo, minio_uploads, heartbeats):
    from pipeline.services import progress as live_progress

    seed_document(db_connection, "wf", stage="ready_for_ingestion", chunks=["x", "y", "z"])
    _ingest("wf", batch_size=2)

    assert len(heartbeats) >= 2
    assert all(event.get("stage") == "ingest" for event in heartbeats)
    last = heartbeats[-1]
    assert {k: last[k] for k in ("phase", "done", "total", "unit", "rows_seen", "rows_total")} == {
        "phase": "ingest", "done": 3, "total": 3, "unit": "chunks", "rows_seen": 3, "rows_total": 3,
    }
    assert last["updated_at"]

    assembled = live_progress.assemble_progress(
        stage="ingesting",
        sqlite={"phase": "ingest", "done": 0, "total": 3, "unit": "chunks"},
        heartbeat=live_progress.normalize_heartbeat(last),
    )
    assert {k: assembled[k] for k in ("done", "total", "unit", "phase")} == {
        "done": 3, "total": 3, "unit": "chunks", "phase": "ingest",
    }
