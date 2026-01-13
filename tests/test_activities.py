"""
Unit tests for pipeline/activities.py - Temporal activities.

Tests cover:
- OCR activity (mocked)
- Chunking activity
- Translation activity (mocked)
- Ingestion activity (mocked)
- State update activity
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import os

os.environ["MISTRAL_API_KEY"] = "test-key"
os.environ["MINIO_ACCESS_KEY"] = "test-access"
os.environ["MINIO_SECRET_KEY"] = "test-secret"


class TestChunkingActivity:
    """Tests for the chunking activity."""

    @pytest.mark.unit
    def test_create_chunks_basic(self):
        """Test basic chunking of pages."""
        from pipeline.activities import create_chunks

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

        chunks = create_chunks(pages, chunk_size=100, chunk_overlap=20, min_tokens=10)

        assert len(chunks) > 0
        for chunk in chunks:
            assert "chunk_number" in chunk
            assert "original_text" in chunk
            assert "source_pages" in chunk
            assert "token_count" in chunk
            assert len(chunk["original_text"]) > 0

    @pytest.mark.unit
    def test_create_chunks_uses_edited_markdown(self):
        """Test that edited_markdown is preferred over original."""
        from pipeline.activities import create_chunks

        pages = [
            {
                "page_number": 1,
                "original_markdown": "Original content that should not appear.",
                "edited_markdown": "Edited content that should appear. " * 30,
                "detected_language": "en"
            }
        ]

        chunks = create_chunks(pages, chunk_size=50, chunk_overlap=10, min_tokens=5)

        assert len(chunks) > 0
        # The edited content should be in the chunks
        all_text = " ".join(c["original_text"] for c in chunks)
        assert "Edited content" in all_text
        assert "Original content that should not appear" not in all_text

    @pytest.mark.unit
    def test_create_chunks_empty_pages(self):
        """Test chunking with empty pages."""
        from pipeline.activities import create_chunks

        pages = []
        chunks = create_chunks(pages, chunk_size=100, chunk_overlap=20, min_tokens=10)
        assert chunks == []

    @pytest.mark.unit
    def test_create_chunks_min_tokens_filter(self):
        """Test that chunks below min_tokens are filtered."""
        from pipeline.activities import create_chunks

        pages = [
            {
                "page_number": 1,
                "original_markdown": "Short.",
                "edited_markdown": None,
                "detected_language": "en"
            }
        ]

        # With high min_tokens, should filter out short chunks
        chunks = create_chunks(pages, chunk_size=100, chunk_overlap=20, min_tokens=100)
        # The short content should be filtered
        assert len(chunks) == 0 or all(c["token_count"] >= 100 for c in chunks)


class TestPrepareIngestionRecords:
    """Tests for the ingestion preparation activity."""

    @pytest.mark.unit
    def test_prepare_records_basic(self):
        """Test preparing records for Marqo ingestion."""
        from pipeline.activities import prepare_ingestion_records

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
        from pipeline.activities import prepare_ingestion_records

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
        from pipeline.activities import prepare_ingestion_records

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
        from pipeline.activities import update_document_state

        workflow_id = "state-update-test"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-state",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="registered"
        )

        update_document_state(
            workflow_id=workflow_id,
            stage="ocr_processing",
            page_count=5,
            chunk_count=0
        )

        doc = db_connection.get_document(workflow_id)
        assert doc["stage"] == "ocr_processing"
        assert doc["page_count"] == 5


class TestMinIOClient:
    """Tests for MinIO client creation."""

    @pytest.mark.unit
    def test_get_minio_client_requires_credentials(self):
        """Test that missing credentials raise error."""
        from pipeline.activities import get_minio_client

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
        from pipeline.activities import get_minio_client

        os.environ["MINIO_ACCESS_KEY"] = "test-key"
        os.environ["MINIO_SECRET_KEY"] = "test-secret"

        client = get_minio_client()
        assert client is not None


class TestOCRActivity:
    """Tests for OCR activity (mocked)."""

    @pytest.mark.unit
    @patch("pipeline.activities.Mistral")
    def test_run_ocr_calls_mistral(self, mock_mistral_class, tmp_path):
        """Test that OCR activity calls Mistral API."""
        from pipeline.activities import run_ocr

        # Create a test PDF file
        pdf_path = tmp_path / "test.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 test content")

        # Mock Mistral client
        mock_client = MagicMock()
        mock_mistral_class.return_value = mock_client

        mock_response = MagicMock()
        mock_response.pages = [
            MagicMock(markdown="# Page 1 content")
        ]
        mock_client.ocr.process.return_value = mock_response

        pages = run_ocr(str(pdf_path))

        assert len(pages) == 1
        assert pages[0]["page_number"] == 1
        assert "# Page 1 content" in pages[0]["original_markdown"]
