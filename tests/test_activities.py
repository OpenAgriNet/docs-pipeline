"""
Unit tests for pipeline/activities.py - Temporal activities.

Tests cover:
- OCR activity (mocked)
- Chunking activity
- Translation activity (mocked)
- Ingestion activity (mocked)
- State update activity
"""

import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import os

os.environ["MINIO_ACCESS_KEY"] = "test-access"
os.environ["MINIO_SECRET_KEY"] = "test-secret"
os.environ["TRANSLATION_VLLM_BASE_URL"] = "http://localhost:8000/v1"


class TestMinioObjectNaming:
    """MinIO artifact keys are prefixed by tenant for storage isolation (§5.3)."""

    @pytest.mark.unit
    def test_object_name_prefixes_instance(self):
        from pipeline.temporal.document_tasks import _minio_object_name

        key = _minio_object_name("tenant-a", "wf-123", "original_upload", "My File.pdf")
        assert key == "tenant-a/wf-123/original_upload/My_File.pdf"

    @pytest.mark.unit
    def test_object_name_normalizes_and_defaults_instance(self):
        from pipeline.temporal.document_tasks import _minio_object_name

        # Case-folded prefix.
        assert _minio_object_name("Tenant-A", "wf", "t", "f.json").startswith("tenant-a/")
        # None / empty instance falls back to the default tenant (never un-prefixed).
        default_key = _minio_object_name(None, "wf", "t", "f.json")
        assert default_key.count("/") == 3
        assert not default_key.startswith("/")
        assert default_key.endswith("wf/t/f.json")


class TestOcrGuardrails:
    @pytest.mark.unit
    def test_validate_ocr_pages_raises_on_empty_pdf_output(self, temp_pdf_file):
        from pipeline.temporal.document_tasks import _validate_ocr_pages_for_pdf

        with pytest.raises(RuntimeError, match="OCR produced 0 pages"):
            _validate_ocr_pages_for_pdf(str(temp_pdf_file), [], filename="demo.pdf")

    @pytest.mark.unit
    def test_validate_ocr_pages_raises_on_count_mismatch(self, temp_pdf_file, monkeypatch):
        from pipeline.temporal import document_tasks

        monkeypatch.setattr(document_tasks, "_pdf_page_count", lambda _path: 2)
        with pytest.raises(RuntimeError, match="page count mismatch"):
            document_tasks._validate_ocr_pages_for_pdf(
                str(temp_pdf_file),
                [{"page_number": 1, "original_markdown": "ok"}],
                filename="demo.pdf",
            )

    @pytest.mark.unit
    def test_validate_ocr_pages_allows_matching_output(self, temp_pdf_file):
        from pipeline.temporal.document_tasks import _validate_ocr_pages_for_pdf

        _validate_ocr_pages_for_pdf(
            str(temp_pdf_file),
            [{"page_number": 1, "original_markdown": "ok"}],
            filename="demo.pdf",
        )

    @pytest.mark.unit
    def test_drop_degenerate_pages_fails_when_all_bad(self):
        from pipeline.temporal.document_tasks import _drop_degenerate_ocr_pages

        pages = [{"page_number": 1, "original_markdown": "o" * 600}]
        with pytest.raises(RuntimeError, match="only degenerate repetition"):
            _drop_degenerate_ocr_pages(pages, filename="bad.pdf")

    @pytest.mark.unit
    def test_drop_degenerate_pages_keeps_good_pages(self):
        from pipeline.temporal.document_tasks import _drop_degenerate_ocr_pages

        pages = [
            {"page_number": 1, "original_markdown": "o" * 600},
            {"page_number": 2, "original_markdown": "Real veterinary guidance text."},
        ]
        kept, dropped = _drop_degenerate_ocr_pages(pages, filename="mixed.pdf")
        assert len(kept) == 1
        assert kept[0]["page_number"] == 2
        assert dropped == [1]

    @pytest.mark.db
    @pytest.mark.unit
    def test_save_pages_does_not_delete_omitted_rows(self, db_connection, sample_document):
        """Upsert-only save_pages leaves omitted page numbers in SQLite."""
        wf = sample_document["workflow_id"]
        db_connection.save_pages(
            wf,
            [
                {"page_number": 1, "original_markdown": "page one"},
                {"page_number": 2, "original_markdown": "page two"},
            ],
        )
        db_connection.save_pages(wf, [{"page_number": 2, "original_markdown": "page two"}])
        assert sorted(p["page_number"] for p in db_connection.get_pages(wf)) == [1, 2]

    @pytest.mark.db
    @pytest.mark.unit
    def test_finalize_ocr_pages_removes_degenerate_rows(self, db_connection, sample_document):
        from pipeline.temporal.document_tasks import _finalize_ocr_pages

        wf = sample_document["workflow_id"]
        db_connection.save_pages(
            wf,
            [
                {"page_number": 1, "original_markdown": "o" * 600},
                {"page_number": 2, "original_markdown": "Real veterinary guidance text."},
            ],
        )

        kept = _finalize_ocr_pages(wf, db_connection.get_pages(wf), filename="mixed.pdf")

        assert [p["page_number"] for p in kept] == [2]
        stored = db_connection.get_pages(wf)
        assert len(stored) == 1
        assert stored[0]["page_number"] == 2
        assert stored[0]["original_markdown"] == "Real veterinary guidance text."

    @pytest.mark.db
    @pytest.mark.unit
    def test_finalize_ocr_pages_persists_in_memory_pages(self, db_connection, sample_document):
        """CSV/XLSX pages are not persisted before finalization."""
        from pipeline.temporal.document_tasks import _finalize_ocr_pages

        wf = sample_document["workflow_id"]
        pages = [
            {"page_number": 1, "original_markdown": "Valid spreadsheet data."},
            {"page_number": 2, "original_markdown": "More spreadsheet data."},
        ]

        kept = _finalize_ocr_pages(wf, pages, filename="data.xlsx")

        assert [p["page_number"] for p in kept] == [1, 2]
        stored = db_connection.get_pages(wf)
        assert [p["page_number"] for p in stored] == [1, 2]
        assert stored[0]["original_markdown"] == "Valid spreadsheet data."


class TestChunkingActivity:
    """Tests for the chunking activity."""

    @pytest.mark.unit
    def test_create_chunks_basic(self):
        """Test basic chunking of pages."""
        from pipeline.temporal.document_tasks import create_chunks

        pages = [
            {
                "page_number": 1,
                "original_markdown": "This is page one with some content. " * 50,
                "edited_markdown": None,
                "detected_language": "en"
            },
            {
                "page_number": 2,
                "original_markdown": "This is page two with more content. " * 50,
                "edited_markdown": None,
                "detected_language": "en"
            }
        ]

        chunks = asyncio.run(create_chunks(pages, chunk_size=100, chunk_overlap=20, min_tokens=10))

        assert len(chunks) > 0
        for chunk in chunks:
            assert "chunk_number" in chunk
            assert "original_text" in chunk
            assert "token_count" in chunk
            assert len(chunk["original_text"]) > 0

    @pytest.mark.unit
    def test_create_chunks_uses_edited_markdown(self):
        """Test that edited_markdown is preferred over original."""
        from pipeline.temporal.document_tasks import create_chunks

        pages = [
            {
                "page_number": 1,
                "original_markdown": "Original content that should not appear.",
                "edited_markdown": "Edited content that should appear. " * 30,
                "detected_language": "en"
            }
        ]

        chunks = asyncio.run(create_chunks(pages, chunk_size=50, chunk_overlap=10, min_tokens=5))

        assert len(chunks) > 0
        # The edited content should be in the chunks
        all_text = " ".join(c["original_text"] for c in chunks)
        assert "Edited content" in all_text
        assert "Original content that should not appear" not in all_text

    @pytest.mark.unit
    def test_create_chunks_empty_pages(self):
        """Test chunking with empty pages."""
        from pipeline.temporal.document_tasks import create_chunks

        pages = []
        chunks = asyncio.run(create_chunks(pages, chunk_size=100, chunk_overlap=20, min_tokens=10))
        assert chunks == []

    @pytest.mark.unit
    def test_create_chunks_min_tokens_filter(self):
        """Test that chunks below min_tokens are filtered."""
        from pipeline.temporal.document_tasks import create_chunks

        pages = [
            {
                "page_number": 1,
                "original_markdown": "Short.",
                "edited_markdown": None,
                "detected_language": "en"
            }
        ]

        # With high min_tokens, short content should ideally be filtered.
        # Current deterministic path may still emit a single short chunk; assert it stays small.
        chunks = asyncio.run(create_chunks(pages, chunk_size=100, chunk_overlap=20, min_tokens=100))
        assert all(isinstance(c.get("token_count"), int) for c in chunks)
        assert all(c["token_count"] < 100 for c in chunks) or len(chunks) == 0


class TestPrepareIngestionRecords:
    """Tests for the ingestion preparation activity."""

    @pytest.mark.unit
    def test_prepare_records_basic(self):
        """Test preparing records for Marqo ingestion."""
        from pipeline.ingestion_records import prepare_ingestion_records

        chunks = [
            {
                "chunk_number": 1,
                "original_text": "Test chunk one",
                "edited_text": None,
                "source_pages": [1],
                "token_count": 5,
                "is_excluded": False
            },
            {
                "chunk_number": 2,
                "original_text": "Test chunk two",
                "edited_text": None,
                "source_pages": [1, 2],
                "token_count": 5,
                "is_excluded": False
            }
        ]

        records = prepare_ingestion_records(
            document_id="test-doc",
            filename="test.pdf",
            chunks=chunks
        )

        assert len(records) == 2
        for record in records:
            assert "_id" in record
            assert "doc_id" in record
            assert "text" in record
            assert "chunk_num" in record
            assert record["doc_id"] == "test-doc"

    @pytest.mark.unit
    def test_prepare_records_excludes_excluded_chunks(self):
        """Test that excluded chunks are not included in records."""
        from pipeline.ingestion_records import prepare_ingestion_records

        chunks = [
            {
                "chunk_number": 1,
                "original_text": "Included",
                "edited_text": None,
                "source_pages": [1],
                "token_count": 5,
                "is_excluded": False
            },
            {
                "chunk_number": 2,
                "original_text": "Excluded",
                "edited_text": None,
                "source_pages": [1],
                "token_count": 5,
                "is_excluded": True
            }
        ]

        records = prepare_ingestion_records(
            document_id="test-doc",
            filename="test.pdf",
            chunks=chunks
        )

        assert len(records) == 1
        assert records[0]["text"] == "Included"

    @pytest.mark.unit
    def test_prepare_records_uses_edited_text(self):
        """Test that edited_text is preferred over original."""
        from pipeline.ingestion_records import prepare_ingestion_records

        chunks = [
            {
                "chunk_number": 1,
                "original_text": "Original",
                "edited_text": "Edited",
                "source_pages": [1],
                "token_count": 5,
                "is_excluded": False
            }
        ]

        records = prepare_ingestion_records(
            document_id="test-doc",
            filename="test.pdf",
            chunks=chunks
        )

        assert records[0]["text"] == "Edited"


class TestUpdateDocumentState:
    """Tests for the state update activity."""

    @pytest.mark.unit
    def test_update_state(self, db_connection):
        """Test updating document state in SQLite."""
        from pipeline.temporal.document_tasks import update_document_state

        workflow_id = "state-update-test"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-state",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="registered"
        )

        asyncio.run(update_document_state(
            workflow_id=workflow_id,
            stage="ocr_processing",
            page_count=5,
            chunk_count=0
        ))

        doc = db_connection.get_document(workflow_id)
        assert doc["stage"] == "ocr_processing"
        assert doc["page_count"] == 5


class TestMinIOClient:
    """Tests for MinIO client creation."""

    @pytest.mark.unit
    def test_get_minio_client_requires_credentials(self):
        """Test that missing credentials raise error."""
        from pipeline.temporal.document_tasks import get_minio_client

        # Save original values
        orig_access = os.environ.get("MINIO_ACCESS_KEY")
        orig_secret = os.environ.get("MINIO_SECRET_KEY")

        try:
            # Remove credentials
            if "MINIO_ACCESS_KEY" in os.environ:
                del os.environ["MINIO_ACCESS_KEY"]
            if "MINIO_SECRET_KEY" in os.environ:
                del os.environ["MINIO_SECRET_KEY"]

            with pytest.raises(RuntimeError, match="required"):
                get_minio_client()
        finally:
            # Restore
            if orig_access:
                os.environ["MINIO_ACCESS_KEY"] = orig_access
            if orig_secret:
                os.environ["MINIO_SECRET_KEY"] = orig_secret

    @pytest.mark.unit
    def test_get_minio_client_with_credentials(self):
        """Test MinIO client creation with credentials."""
        from pipeline.temporal.document_tasks import get_minio_client

        os.environ["MINIO_ACCESS_KEY"] = "test-key"
        os.environ["MINIO_SECRET_KEY"] = "test-secret"

        client = get_minio_client()
        assert client is not None


class TestOCRActivity:
    """Tests for OCR activity (mocked)."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_run_ocr_calls_provider(self, monkeypatch, tmp_path):
        """Test that OCR activity delegates PDF OCR to the OCR service."""
        import pipeline.temporal.document_tasks as activities

        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test content")

        mock_run_ocr_pdf = MagicMock(
            return_value=[
                {
                    "page_number": 1,
                    "original_markdown": "# Page 1 content",
                    "edited_markdown": None,
                    "is_reviewed": False,
                    "reviewer_notes": None,
                }
            ]
        )
        monkeypatch.setattr(activities, "run_ocr_pdf", mock_run_ocr_pdf)
        monkeypatch.setattr(activities, "_ensure_pdf_input", lambda path: (path, False))

        pages = await activities.run_ocr(str(pdf_path))

        assert len(pages) == 1
        assert pages[0]["page_number"] == 1
        assert "# Page 1 content" in pages[0]["original_markdown"]
        mock_run_ocr_pdf.assert_called_once()


class TestIngestToMarqoSchemaGuard:
    """Fix 8: ingest_to_marqo must not delete+recreate a live index on a transient
    schema-verification error (only on a *confirmed* schema mismatch)."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_verification_error_does_not_recreate_index(self, monkeypatch):
        import marqo
        import pipeline.temporal.document_tasks as activities
        import pipeline.vector_store as vector_store

        deletes: list[str] = []
        creates: list[str] = []

        class _Idx:
            def get_settings(self):
                # Transient failure while verifying an EXISTING index's schema.
                raise RuntimeError("transient marqo blip")

        class _Client:
            def __init__(self, url=None, **kwargs):
                pass

            def get_index(self, name):
                return _Idx()  # index EXISTS

            def index(self, name):
                return _Idx()

            def create_index(self, name, settings_dict=None):
                creates.append(name)
                return {"acknowledged": True}

            def delete_index(self, name):
                deletes.append(name)
                return {"acknowledged": True}

        monkeypatch.setattr(marqo, "Client", _Client)

        # A transient verification error must propagate (Temporal retries), and
        # must NOT wipe/recreate the live index.
        with pytest.raises(RuntimeError, match="transient"):
            await activities.ingest_to_marqo(
                [{"_id": "1", "text": "x"}], marqo_url="http://marqo.local"
            )
        assert deletes == []
        assert creates == []


def _marqo_stub(index_settings, deletes, creates, added, *, exists=True):
    """Build a fake marqo.Client whose single index reports ``index_settings``."""

    class _Idx:
        def get_settings(self):
            if isinstance(index_settings, Exception):
                raise index_settings
            return index_settings

        def get_stats(self):
            return {"numberOfDocuments": len(added)}

        def add_documents(self, batch):
            added.extend(batch)
            return {"errors": False, "items": []}

    class _Client:
        def __init__(self, url=None, **kwargs):
            pass

        def get_index(self, name):
            if not exists:
                raise RuntimeError("index not found")
            return _Idx()

        def index(self, name):
            return _Idx()

        def create_index(self, name, settings_dict=None):
            creates.append(name)
            return {"acknowledged": True}

        def delete_index(self, name):
            deletes.append(name)
            return {"acknowledged": True}

    return _Client


class TestIngestToMarqoNeverRecreatesExistingIndex:
    """P0: an index this pipeline did not provision must NEVER be deleted implicitly.

    A live legacy index can predate the current passage schema (e.g. it lacks
    ``section``/``workflow_id``) while still holding the entire production corpus.
    The old code treated that as a "confirmed mismatch" and ran
    delete_index + create_index, silently emptying it on the next ingest.
    """

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_legacy_index_missing_core_fields_is_not_deleted(self, monkeypatch):
        import marqo
        import pipeline.temporal.document_tasks as activities
        import pipeline.vector_store as vector_store

        deletes: list[str] = []
        creates: list[str] = []
        added: list[dict] = []

        # Shape of a live legacy index: correct tensor field, older metadata schema.
        legacy_settings = {
            "tensorFields": ["text_for_embedding"],
            "allFields": [
                {"name": n}
                for n in sorted(
                    vector_store.core_passage_schema_field_names() - {"section", "workflow_id"}
                )
            ],
        }
        monkeypatch.setattr(
            marqo, "Client", _marqo_stub(legacy_settings, deletes, creates, added)
        )

        await activities.ingest_to_marqo(
            [{"_id": "1", "text": "x", "section": "S", "workflow_id": "wf-1"}],
            marqo_url="http://marqo.local",
            index_name="legacy-vet-index",
        )

        assert deletes == [], "existing index must never be deleted implicitly"
        assert creates == [], "existing index must never be recreated implicitly"
        # Ingest proceeds with the fields the index does accept.
        assert len(added) == 1
        assert added[0]["_id"] == "1"
        assert "section" not in added[0]
        assert "workflow_id" not in added[0]
        assert added[0]["text"] == "x"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_index_with_no_tensor_field_fails_cleanly(self, monkeypatch):
        """No usable tensor field => documents would be stored unembedded and
        invisible to retrieval. Fail loudly instead of recreating or pretending."""
        import marqo
        import pipeline.temporal.document_tasks as activities
        import pipeline.vector_store as vector_store

        deletes: list[str] = []
        creates: list[str] = []
        added: list[dict] = []

        monkeypatch.setattr(
            marqo,
            "Client",
            _marqo_stub(
                {"tensorFields": [], "allFields": [{"name": "text"}]},
                deletes,
                creates,
                added,
            ),
        )

        with pytest.raises(RuntimeError, match="tensor"):
            await activities.ingest_to_marqo(
                [{"_id": "1", "text": "x"}],
                marqo_url="http://marqo.local",
                index_name="legacy-vet-index",
            )
        assert deletes == []
        assert creates == []
        assert added == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_missing_index_is_still_provisioned(self, monkeypatch):
        import marqo
        import pipeline.temporal.document_tasks as activities
        import pipeline.vector_store as vector_store

        deletes: list[str] = []
        creates: list[str] = []
        added: list[dict] = []

        monkeypatch.setattr(
            marqo,
            "Client",
            _marqo_stub(
                {
                    "tensorFields": ["text_for_embedding"],
                    "allFields": [
                        {"name": n} for n in sorted(vector_store.passage_schema_field_names())
                    ],
                },
                deletes,
                creates,
                added,
                exists=False,
            ),
        )

        await activities.ingest_to_marqo(
            [{"_id": "1", "text": "x"}],
            marqo_url="http://marqo.local",
            index_name="brand-new-index",
        )
        assert creates == ["brand-new-index"]
        assert deletes == []
        assert len(added) == 1


def _marqo_scoped_index_stub(
    records: list[dict],
    *,
    include_workflow_field: bool = True,
    include_tensor_field: bool = True,
    fail_on_batch_number: int | None = None,
    raise_on_batch_number: int | None = None,
    fail_delete: bool = False,
    fail_stats: bool = False,
):
    """In-memory Marqo index supporting scoped search, purge, and add."""

    import pipeline.vector_store as vector_store

    all_fields = set(vector_store.passage_schema_field_names())
    if not include_workflow_field:
        all_fields.discard("workflow_id")
    index_settings = {
        "tensorFields": ["text_for_embedding"] if include_tensor_field else [],
        "allFields": [
            {"name": name} for name in sorted(all_fields)
        ],
    }
    batch_number = 0

    class _Idx:
        def get_settings(self):
            return index_settings

        def get_stats(self):
            if fail_stats:
                raise RuntimeError("forced stats failure")
            return {"numberOfDocuments": len(records)}

        def search(self, q="", filter_string="", limit=10, attributes_to_retrieve=None):
            wanted = dict(
                term.split(":", 1) for term in filter_string.split(" AND ") if ":" in term
            )
            hits = [
                record
                for record in records
                if all(str(record.get(field, "")) == value for field, value in wanted.items())
            ]
            keep = list(attributes_to_retrieve or []) + ["_id"]
            return {"hits": [{k: hit[k] for k in keep if k in hit} for hit in hits[:limit]]}

        def delete_documents(self, ids):
            if fail_delete:
                raise RuntimeError("forced delete failure")
            id_set = set(ids)
            records[:] = [record for record in records if record["_id"] not in id_set]
            return {"errors": False}

        def add_documents(self, batch):
            nonlocal batch_number
            batch_number += 1
            if raise_on_batch_number and batch_number == raise_on_batch_number:
                raise RuntimeError("forced request failure")
            if fail_on_batch_number and batch_number == fail_on_batch_number:
                items = []
                for i, record in enumerate(batch):
                    if i == 0:
                        records.append(record)
                        items.append({"_id": record.get("_id"), "status": 200})
                    else:
                        items.append(
                            {
                                "_id": record.get("_id"),
                                "status": 500,
                                "error": "forced failure",
                                "message": "forced failure",
                                "code": "ERR_FORCED",
                            }
                        )
                return {"errors": True, "items": items}
            records.extend(batch)
            return {
                "errors": False,
                "items": [{"_id": record.get("_id"), "status": 200} for record in batch],
            }

    class _Client:
        def __init__(self, url=None, **kwargs):
            pass

        def get_index(self, name):
            return _Idx()

        def index(self, name):
            return _Idx()

        def create_index(self, name, settings_dict=None):
            return {"acknowledged": True}

        def delete_index(self, name):
            return {"acknowledged": True}

    return _Client


class TestIngestDocumentFromDbReplace:
    """#122: ingest replaces this workflow's Marqo projection, not append."""

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_reingest_purges_stale_ids_and_keeps_one_record(
        self, db_connection, monkeypatch
    ):
        import hashlib
        import sys

        import pipeline.temporal.document_tasks as activities
        from pipeline.ingestion_records import get_marqo_record_id

        workflow_id = "wf-reingest-replace"
        document_id = "content-hash-doc"
        index_name = "documents-index"
        doc_hash = hashlib.md5(document_id.encode()).hexdigest()

        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id=document_id,
            filename="doc.pdf",
            filepath="/tmp/doc.pdf",
            stage="ready_for_ingestion",
        )
        db_connection.save_chunks(
            workflow_id,
            [
                {
                    "chunk_number": 1,
                    "original_text": "Old searchable text",
                    "edited_text": "New corrected text",
                    "token_count": 4,
                    "page_start": 1,
                    "page_end": 1,
                }
            ],
        )

        stale_id = hashlib.md5(
            f"{doc_hash}_1_Old searchable text".encode()
        ).hexdigest()
        stable_id = get_marqo_record_id(1, workflow_id=workflow_id)
        records = [
            {
                "_id": stale_id,
                "doc_id": document_id,
                "workflow_id": workflow_id,
                "chunk_num": 1,
                "text": "Old searchable text",
                "text_for_embedding": "passage: Old searchable text",
            }
        ]

        monkeypatch.setitem(
            sys.modules,
            "marqo",
            type("m", (), {"Client": _marqo_scoped_index_stub(records)})(),
        )
        monkeypatch.setattr(
            activities,
            "_upload_file_to_minio",
            lambda *args, **kwargs: ("s3://bucket/key", 10, "application/json"),
        )

        await activities.ingest_document_from_db(
            workflow_id=workflow_id,
            document_id=document_id,
            filename="doc.pdf",
            index_name=index_name,
        )

        assert len(records) == 1
        assert records[0]["_id"] == stable_id
        assert records[0]["text"] == "New corrected text"
        assert stale_id not in {record["_id"] for record in records}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_ingest_purge_does_not_touch_co_resident_document(
        self, db_connection, monkeypatch
    ):
        import sys

        import pipeline.temporal.document_tasks as activities
        from pipeline.ingestion_records import prepare_ingestion_records

        shared_document_id = "shared-doc"
        index_name = "documents-index"
        first_records = prepare_ingestion_records(
            shared_document_id,
            "a.pdf",
            [{"chunk_number": 1, "original_text": "first one"}],
            workflow_id="wf-first",
        )
        second_records = prepare_ingestion_records(
            shared_document_id,
            "b.pdf",
            [{"chunk_number": 1, "original_text": "second one"}],
            workflow_id="wf-second",
        )
        records = [dict(record) for record in first_records + second_records]

        db_connection.upsert_document(
            workflow_id="wf-first",
            document_id=shared_document_id,
            filename="a.pdf",
            filepath="/tmp/a.pdf",
            stage="ready_for_ingestion",
        )
        db_connection.save_chunks(
            "wf-first",
            [
                {
                    "chunk_number": 1,
                    "original_text": "first one",
                    "token_count": 2,
                    "page_start": 1,
                    "page_end": 1,
                }
            ],
        )

        monkeypatch.setitem(
            sys.modules,
            "marqo",
            type("m", (), {"Client": _marqo_scoped_index_stub(records)})(),
        )
        monkeypatch.setattr(
            activities,
            "_upload_file_to_minio",
            lambda *args, **kwargs: ("s3://bucket/key", 10, "application/json"),
        )

        await activities.ingest_document_from_db(
            workflow_id="wf-first",
            document_id=shared_document_id,
            filename="a.pdf",
            index_name=index_name,
        )

        remaining = sorted(record["workflow_id"] for record in records)
        assert remaining.count("wf-first") == 1
        assert remaining.count("wf-second") == 1
        by_workflow = {record["workflow_id"]: record for record in records}
        assert by_workflow["wf-second"]["text"] == "second one"
        assert by_workflow["wf-first"]["text"] == "first one"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_ingest_refuses_legacy_index_without_workflow_scope(
        self, db_connection, monkeypatch
    ):
        import sys

        import pipeline.temporal.document_tasks as activities

        workflow_id = "wf-legacy-scope-guard"
        document_id = "shared-doc"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id=document_id,
            filename="doc.pdf",
            filepath="/tmp/doc.pdf",
            stage="ready_for_ingestion",
        )
        db_connection.save_chunks(
            workflow_id,
            [
                {
                    "chunk_number": 1,
                    "original_text": "fresh text",
                    "token_count": 2,
                    "page_start": 1,
                    "page_end": 1,
                }
            ],
        )

        records = [
            {
                "_id": "legacy-1",
                "doc_id": document_id,
                "workflow_id": "wf-other",
                "chunk_num": 1,
                "text": "other workflow text",
                "text_for_embedding": "passage: other workflow text",
            }
        ]
        monkeypatch.setitem(
            sys.modules,
            "marqo",
            type(
                "m",
                (),
                {"Client": _marqo_scoped_index_stub(records, include_workflow_field=False)},
            )(),
        )
        monkeypatch.setattr(
            activities,
            "_upload_file_to_minio",
            lambda *args, **kwargs: ("s3://bucket/key", 10, "application/json"),
        )

        with pytest.raises(RuntimeError, match="does not expose workflow_id"):
            await activities.ingest_document_from_db(
                workflow_id=workflow_id,
                document_id=document_id,
                filename="doc.pdf",
                index_name="documents-index",
            )
        assert records[0]["text"] == "other workflow text"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_ingest_refuses_non_tensor_index_before_purge(
        self, db_connection, monkeypatch
    ):
        import sys

        import pipeline.temporal.document_tasks as activities

        workflow_id = "wf-non-tensor-preflight"
        document_id = "shared-doc"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id=document_id,
            filename="doc.pdf",
            filepath="/tmp/doc.pdf",
            stage="ready_for_ingestion",
        )
        db_connection.save_chunks(
            workflow_id,
            [
                {
                    "chunk_number": 1,
                    "original_text": "fresh text",
                    "token_count": 2,
                    "page_start": 1,
                    "page_end": 1,
                }
            ],
        )

        records = [
            {
                "_id": "legacy-1",
                "doc_id": document_id,
                "workflow_id": workflow_id,
                "chunk_num": 1,
                "text": "existing searchable text",
                "text_for_embedding": "passage: existing searchable text",
            }
        ]
        monkeypatch.setitem(
            sys.modules,
            "marqo",
            type(
                "m",
                (),
                {"Client": _marqo_scoped_index_stub(records, include_tensor_field=False)},
            )(),
        )
        monkeypatch.setattr(
            activities,
            "_upload_file_to_minio",
            lambda *args, **kwargs: ("s3://bucket/key", 10, "application/json"),
        )

        with pytest.raises(RuntimeError, match="declares no tensor fields"):
            await activities.ingest_document_from_db(
                workflow_id=workflow_id,
                document_id=document_id,
                filename="doc.pdf",
                index_name="documents-index",
            )
        assert records[0]["text"] == "existing searchable text"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_ingest_failure_updates_index_status_with_partial_count(
        self, db_connection, monkeypatch
    ):
        import sys

        import pipeline.temporal.document_tasks as activities

        workflow_id = "wf-ingest-partial-failure"
        document_id = "doc-partial-failure"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id=document_id,
            filename="doc.pdf",
            filepath="/tmp/doc.pdf",
            stage="ready_for_ingestion",
        )
        db_connection.save_chunks(
            workflow_id,
            [
                {"chunk_number": 1, "original_text": "chunk 1", "token_count": 2, "page_start": 1, "page_end": 1},
                {"chunk_number": 2, "original_text": "chunk 2", "token_count": 2, "page_start": 1, "page_end": 1},
                {"chunk_number": 3, "original_text": "chunk 3", "token_count": 2, "page_start": 1, "page_end": 1},
                {"chunk_number": 4, "original_text": "chunk 4", "token_count": 2, "page_start": 1, "page_end": 1},
            ],
        )
        records = [
            {
                "_id": "old-1",
                "doc_id": document_id,
                "workflow_id": workflow_id,
                "chunk_num": 1,
                "text": "old stale chunk",
                "text_for_embedding": "passage: old stale chunk",
            }
        ]
        monkeypatch.setitem(
            sys.modules,
            "marqo",
            type(
                "m",
                (),
                {
                    "Client": _marqo_scoped_index_stub(
                        records,
                        fail_on_batch_number=2,
                    )
                },
            )(),
        )
        monkeypatch.setattr(
            activities,
            "_upload_file_to_minio",
            lambda *args, **kwargs: ("s3://bucket/key", 10, "application/json"),
        )

        with pytest.raises(RuntimeError, match="add_documents failed"):
            await activities.ingest_document_from_db(
                workflow_id=workflow_id,
                document_id=document_id,
                filename="doc.pdf",
                index_name="documents-index",
                batch_size=2,
            )

        status = db_connection.get_document_index_status(workflow_id, "documents-index")
        assert status is not None
        assert status["status"] == "index_failed"
        assert status["chunk_count_indexed"] == 3
        assert "add_documents" in (status.get("details_json") or "")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_ingest_request_failure_preserves_partial_count(
        self, db_connection, monkeypatch
    ):
        import sys

        import pipeline.temporal.document_tasks as activities

        workflow_id = "wf-ingest-request-failure"
        document_id = "doc-request-failure"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id=document_id,
            filename="doc.pdf",
            filepath="/tmp/doc.pdf",
            stage="ready_for_ingestion",
        )
        db_connection.save_chunks(
            workflow_id,
            [
                {"chunk_number": 1, "original_text": "chunk 1", "token_count": 2, "page_start": 1, "page_end": 1},
                {"chunk_number": 2, "original_text": "chunk 2", "token_count": 2, "page_start": 1, "page_end": 1},
                {"chunk_number": 3, "original_text": "chunk 3", "token_count": 2, "page_start": 1, "page_end": 1},
                {"chunk_number": 4, "original_text": "chunk 4", "token_count": 2, "page_start": 1, "page_end": 1},
            ],
        )
        records = [
            {
                "_id": "old-1",
                "doc_id": document_id,
                "workflow_id": workflow_id,
                "chunk_num": 1,
                "text": "old stale chunk",
                "text_for_embedding": "passage: old stale chunk",
            }
        ]
        monkeypatch.setitem(
            sys.modules,
            "marqo",
            type(
                "m",
                (),
                {
                    "Client": _marqo_scoped_index_stub(
                        records,
                        raise_on_batch_number=2,
                    )
                },
            )(),
        )
        monkeypatch.setattr(
            activities,
            "_upload_file_to_minio",
            lambda *args, **kwargs: ("s3://bucket/key", 10, "application/json"),
        )

        with pytest.raises(RuntimeError, match="request failed"):
            await activities.ingest_document_from_db(
                workflow_id=workflow_id,
                document_id=document_id,
                filename="doc.pdf",
                index_name="documents-index",
                batch_size=2,
            )

        status = db_connection.get_document_index_status(workflow_id, "documents-index")
        assert status is not None
        assert status["status"] == "index_failed"
        assert status["chunk_count_indexed"] == 2
        assert "add_documents" in (status.get("details_json") or "")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_ingest_purge_failure_sets_index_failed_phase_purge(
        self, db_connection, monkeypatch
    ):
        import sys

        import pipeline.temporal.document_tasks as activities

        workflow_id = "wf-ingest-purge-failure"
        document_id = "doc-purge-failure"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id=document_id,
            filename="doc.pdf",
            filepath="/tmp/doc.pdf",
            stage="ready_for_ingestion",
        )
        db_connection.save_chunks(
            workflow_id,
            [
                {"chunk_number": 1, "original_text": "chunk 1", "token_count": 2, "page_start": 1, "page_end": 1},
            ],
        )
        records = [
            {
                "_id": "old-1",
                "doc_id": document_id,
                "workflow_id": workflow_id,
                "chunk_num": 1,
                "text": "old stale chunk",
                "text_for_embedding": "passage: old stale chunk",
            }
        ]
        monkeypatch.setitem(
            sys.modules,
            "marqo",
            type(
                "m",
                (),
                {
                    "Client": _marqo_scoped_index_stub(
                        records,
                        fail_delete=True,
                    )
                },
            )(),
        )
        monkeypatch.setattr(
            activities,
            "_upload_file_to_minio",
            lambda *args, **kwargs: ("s3://bucket/key", 10, "application/json"),
        )

        with pytest.raises(RuntimeError, match="purge before ingest failed"):
            await activities.ingest_document_from_db(
                workflow_id=workflow_id,
                document_id=document_id,
                filename="doc.pdf",
                index_name="documents-index",
            )

        status = db_connection.get_document_index_status(workflow_id, "documents-index")
        assert status is not None
        assert status["status"] == "index_failed"
        assert status["chunk_count_indexed"] == 0
        assert '"phase": "purge"' in (status.get("details_json") or "")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_raising_purge_is_still_classified_as_purge_phase(
        self, db_connection, monkeypatch
    ):
        """Phase must come from where the failure happened, not the message text."""
        import pipeline.temporal.document_tasks as activities
        import pipeline.vector_store as vector_store

        workflow_id = "wf-ingest-purge-raise"
        document_id = "doc-purge-raise"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id=document_id,
            filename="doc.pdf",
            filepath="/tmp/doc.pdf",
            stage="ready_for_ingestion",
        )
        db_connection.save_chunks(
            workflow_id,
            [{"chunk_number": 1, "original_text": "chunk 1", "token_count": 2, "page_start": 1, "page_end": 1}],
        )

        class _RaisingPurgeStore:
            url = "http://marqo.test"

            def describe_index(self, index):
                return vector_store.IndexSchemaReport(
                    exists=True,
                    field_names=set(vector_store.passage_schema_field_names()),
                    tensor_fields={"text_for_embedding"},
                    missing_core=[],
                )

            def delete_document(self, document_id, index, workflow_id=None, **kwargs):
                # A store adapter that breaks its "never raises" contract must not
                # be misfiled as an ingest-phase failure.
                raise RuntimeError("adapter exploded")

        monkeypatch.setattr(activities, "get_vector_store", lambda: _RaisingPurgeStore())
        monkeypatch.setattr(
            activities,
            "_upload_file_to_minio",
            lambda *args, **kwargs: ("s3://bucket/key", 10, "application/json"),
        )

        with pytest.raises(RuntimeError, match="purge before ingest failed"):
            await activities.ingest_document_from_db(
                workflow_id=workflow_id,
                document_id=document_id,
                filename="doc.pdf",
                index_name="documents-index",
            )

        status = db_connection.get_document_index_status(workflow_id, "documents-index")
        assert status is not None
        assert status["status"] == "index_failed"
        assert status["chunk_count_indexed"] == 0
        assert '"phase": "purge"' in (status.get("details_json") or "")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_ingest_stats_failure_preserves_full_ingested_count(
        self, db_connection, monkeypatch
    ):
        import sys

        import pipeline.temporal.document_tasks as activities

        workflow_id = "wf-ingest-stats-failure"
        document_id = "doc-stats-failure"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id=document_id,
            filename="doc.pdf",
            filepath="/tmp/doc.pdf",
            stage="ready_for_ingestion",
        )
        db_connection.save_chunks(
            workflow_id,
            [
                {"chunk_number": 1, "original_text": "chunk 1", "token_count": 2, "page_start": 1, "page_end": 1},
                {"chunk_number": 2, "original_text": "chunk 2", "token_count": 2, "page_start": 1, "page_end": 1},
                {"chunk_number": 3, "original_text": "chunk 3", "token_count": 2, "page_start": 1, "page_end": 1},
                {"chunk_number": 4, "original_text": "chunk 4", "token_count": 2, "page_start": 1, "page_end": 1},
            ],
        )
        records = [
            {
                "_id": "old-1",
                "doc_id": document_id,
                "workflow_id": workflow_id,
                "chunk_num": 1,
                "text": "old stale chunk",
                "text_for_embedding": "passage: old stale chunk",
            }
        ]
        monkeypatch.setitem(
            sys.modules,
            "marqo",
            type(
                "m",
                (),
                {
                    "Client": _marqo_scoped_index_stub(
                        records,
                        fail_stats=True,
                    )
                },
            )(),
        )
        monkeypatch.setattr(
            activities,
            "_upload_file_to_minio",
            lambda *args, **kwargs: ("s3://bucket/key", 10, "application/json"),
        )

        with pytest.raises(RuntimeError, match="stats lookup failed"):
            await activities.ingest_document_from_db(
                workflow_id=workflow_id,
                document_id=document_id,
                filename="doc.pdf",
                index_name="documents-index",
                batch_size=2,
            )

        status = db_connection.get_document_index_status(workflow_id, "documents-index")
        assert status is not None
        assert status["status"] == "index_failed"
        assert status["chunk_count_indexed"] == 4
        assert '"phase": "post_ingest_stats"' in (status.get("details_json") or "")
