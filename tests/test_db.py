"""
Unit tests for pipeline/db.py - SQLite database operations.

Tests cover:
- Document CRUD operations
- Page operations
- Chunk operations
- Audit logging
- Settings management
"""

import pytest
from datetime import datetime


class TestDocumentOperations:
    """Tests for document CRUD operations."""

    @pytest.mark.db
    @pytest.mark.unit
    def test_upsert_document_creates_new(self, db_connection):
        """Test creating a new document."""
        workflow_id = "test-wf-001"
        document_id = "test-doc-001"

        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id=document_id,
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="registered"
        )

        doc = db_connection.get_document(workflow_id)
        assert doc is not None
        assert doc["workflow_id"] == workflow_id
        assert doc["document_id"] == document_id
        assert doc["filename"] == "test.pdf"
        assert doc["stage"] == "registered"

    @pytest.mark.db
    @pytest.mark.unit
    def test_upsert_document_updates_existing(self, db_connection):
        """Test updating an existing document."""
        workflow_id = "test-wf-002"

        # Create initial document
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-002",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="registered"
        )

        # Update it
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-002",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="ocr_processing"
        )

        doc = db_connection.get_document(workflow_id)
        assert doc["stage"] == "ocr_processing"

    @pytest.mark.db
    @pytest.mark.unit
    def test_get_document_not_found(self, db_connection):
        """Test getting a non-existent document returns None."""
        doc = db_connection.get_document("nonexistent-workflow")
        assert doc is None

    @pytest.mark.db
    @pytest.mark.unit
    def test_list_documents(self, db_connection):
        """Test listing documents."""
        # Create multiple documents
        for i in range(3):
            db_connection.upsert_document(
                workflow_id=f"list-test-{i}",
                document_id=f"doc-{i}",
                filename=f"test{i}.pdf",
                filepath=f"/app/books/test{i}.pdf",
                stage="registered"
            )

        docs = db_connection.list_documents()
        assert len(docs) >= 3

    @pytest.mark.db
    @pytest.mark.unit
    def test_list_documents_with_stage_filter(self, db_connection):
        """Test filtering documents by stage."""
        db_connection.upsert_document(
            workflow_id="filter-test-1",
            document_id="doc-filter-1",
            filename="test1.pdf",
            filepath="/app/books/test1.pdf",
            stage="completed"
        )
        db_connection.upsert_document(
            workflow_id="filter-test-2",
            document_id="doc-filter-2",
            filename="test2.pdf",
            filepath="/app/books/test2.pdf",
            stage="failed"
        )

        completed_docs = db_connection.list_documents(stage="completed")
        for doc in completed_docs:
            assert doc["stage"] == "completed"

    @pytest.mark.db
    @pytest.mark.unit
    def test_update_document_stage(self, db_connection):
        """Test updating document stage."""
        workflow_id = "stage-test"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-stage",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="registered"
        )

        db_connection.update_document_stage(
            workflow_id=workflow_id,
            stage="ocr_review",
            page_count=5,
            chunk_count=10
        )

        doc = db_connection.get_document(workflow_id)
        assert doc["stage"] == "ocr_review"
        assert doc["page_count"] == 5
        assert doc["chunk_count"] == 10

    @pytest.mark.db
    @pytest.mark.unit
    def test_set_document_demo(self, db_connection):
        """Test marking document as demo."""
        workflow_id = "demo-test"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-demo",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="registered"
        )

        db_connection.set_document_demo(workflow_id, True)
        doc = db_connection.get_document(workflow_id)
        assert doc["is_demo"] == 1

    @pytest.mark.db
    @pytest.mark.unit
    def test_set_document_disabled(self, db_connection):
        """Test soft-deleting document."""
        workflow_id = "disable-test"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-disable",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="registered"
        )

        db_connection.set_document_disabled(workflow_id, True)
        doc = db_connection.get_document(workflow_id)
        assert doc["is_disabled"] == 1


class TestPageOperations:
    """Tests for page CRUD operations."""

    @pytest.mark.db
    @pytest.mark.unit
    def test_persist_pages(self, db_connection, sample_document):
        """Test persisting pages to database."""
        pages = [
            {
                "page_number": 1,
                "original_markdown": "# Page 1",
                "detected_language": "en"
            },
            {
                "page_number": 2,
                "original_markdown": "# Page 2",
                "detected_language": "en"
            }
        ]

        db_connection.persist_document_content(
            workflow_id=sample_document["workflow_id"],
            pages=pages,
            chunks=[]
        )

        stored_pages = db_connection.get_pages(sample_document["workflow_id"])
        assert len(stored_pages) == 2
        assert stored_pages[0]["original_markdown"] == "# Page 1"

    @pytest.mark.db
    @pytest.mark.unit
    def test_get_page(self, db_connection, sample_document):
        """Test getting a specific page."""
        pages = [{"page_number": 1, "original_markdown": "Test", "detected_language": "en"}]
        db_connection.persist_document_content(
            workflow_id=sample_document["workflow_id"],
            pages=pages,
            chunks=[]
        )

        page = db_connection.get_page(sample_document["workflow_id"], 1)
        assert page is not None
        assert page["page_number"] == 1

    @pytest.mark.db
    @pytest.mark.unit
    def test_update_page(self, db_connection, sample_document):
        """Test updating a page."""
        pages = [{"page_number": 1, "original_markdown": "Original", "detected_language": "en"}]
        db_connection.persist_document_content(
            workflow_id=sample_document["workflow_id"],
            pages=pages,
            chunks=[]
        )

        db_connection.update_page(
            workflow_id=sample_document["workflow_id"],
            page_num=1,
            edited_markdown="Edited content",
            is_reviewed=True
        )

        page = db_connection.get_page(sample_document["workflow_id"], 1)
        assert page["edited_markdown"] == "Edited content"
        assert page["is_reviewed"] == 1

    @pytest.mark.db
    @pytest.mark.unit
    def test_reset_page(self, db_connection, sample_document):
        """Test resetting a page to original."""
        pages = [{"page_number": 1, "original_markdown": "Original", "detected_language": "en"}]
        db_connection.persist_document_content(
            workflow_id=sample_document["workflow_id"],
            pages=pages,
            chunks=[]
        )

        # Edit then reset
        db_connection.update_page(
            workflow_id=sample_document["workflow_id"],
            page_num=1,
            edited_markdown="Edited"
        )
        db_connection.reset_page(sample_document["workflow_id"], 1)

        page = db_connection.get_page(sample_document["workflow_id"], 1)
        assert page["edited_markdown"] is None


class TestChunkOperations:
    """Tests for chunk CRUD operations."""

    @pytest.mark.db
    @pytest.mark.unit
    def test_persist_chunks(self, db_connection, sample_document):
        """Test persisting chunks to database."""
        chunks = [
            {
                "chunk_number": 1,
                "original_text": "Chunk 1 text",
                "source_pages": [1],
                "token_count": 5
            },
            {
                "chunk_number": 2,
                "original_text": "Chunk 2 text",
                "source_pages": [1, 2],
                "token_count": 5
            }
        ]

        db_connection.persist_document_content(
            workflow_id=sample_document["workflow_id"],
            pages=[],
            chunks=chunks
        )

        stored_chunks = db_connection.get_chunks(sample_document["workflow_id"])
        assert len(stored_chunks) == 2

    @pytest.mark.db
    @pytest.mark.unit
    def test_get_chunks_excludes_excluded_by_default(self, db_connection, sample_document):
        """Test that excluded chunks are filtered out by default."""
        chunks = [
            {"chunk_number": 1, "original_text": "Included", "source_pages": [1], "token_count": 5},
            {"chunk_number": 2, "original_text": "Excluded", "source_pages": [1], "token_count": 5}
        ]
        db_connection.persist_document_content(
            workflow_id=sample_document["workflow_id"],
            pages=[],
            chunks=chunks
        )

        # Exclude chunk 2
        db_connection.update_chunk(
            workflow_id=sample_document["workflow_id"],
            chunk_num=2,
            is_excluded=True
        )

        # Default: should not include excluded
        chunks_filtered = db_connection.get_chunks(sample_document["workflow_id"])
        assert len(chunks_filtered) == 1

        # With include_excluded=True
        all_chunks = db_connection.get_chunks(sample_document["workflow_id"], include_excluded=True)
        assert len(all_chunks) == 2

    @pytest.mark.db
    @pytest.mark.unit
    def test_update_chunk(self, db_connection, sample_document):
        """Test updating a chunk."""
        chunks = [{"chunk_number": 1, "original_text": "Original", "source_pages": [1], "token_count": 5}]
        db_connection.persist_document_content(
            workflow_id=sample_document["workflow_id"],
            pages=[],
            chunks=chunks
        )

        db_connection.update_chunk(
            workflow_id=sample_document["workflow_id"],
            chunk_num=1,
            edited_text="Edited text",
            is_reviewed=True
        )

        chunk = db_connection.get_chunk(sample_document["workflow_id"], 1)
        assert chunk["edited_text"] == "Edited text"
        assert chunk["is_reviewed"] == 1


class TestAuditLogging:
    """Tests for audit logging functionality."""

    @pytest.mark.db
    @pytest.mark.unit
    def test_log_audit(self, db_connection, sample_document):
        """Test creating audit log entry."""
        db_connection.log_audit(
            workflow_id=sample_document["workflow_id"],
            document_id=sample_document["document_id"],
            action_type="test_action",
            entity_type="document",
            field_name="stage",
            old_value="old",
            new_value="new"
        )

        logs = db_connection.get_audit_logs(sample_document["workflow_id"])
        assert len(logs) >= 1
        assert logs[0]["action_type"] == "test_action"

    @pytest.mark.db
    @pytest.mark.unit
    def test_get_audit_logs_with_filter(self, db_connection, sample_document):
        """Test filtering audit logs by action type."""
        db_connection.log_audit(
            workflow_id=sample_document["workflow_id"],
            document_id=sample_document["document_id"],
            action_type="type_a"
        )
        db_connection.log_audit(
            workflow_id=sample_document["workflow_id"],
            document_id=sample_document["document_id"],
            action_type="type_b"
        )

        logs_a = db_connection.get_audit_logs(
            sample_document["workflow_id"],
            action_type="type_a"
        )
        assert all(log["action_type"] == "type_a" for log in logs_a)

    @pytest.mark.db
    @pytest.mark.unit
    def test_get_all_audit_logs(self, db_connection, sample_document):
        """Test getting global audit logs."""
        db_connection.log_audit(
            workflow_id=sample_document["workflow_id"],
            document_id=sample_document["document_id"],
            action_type="global_test"
        )

        logs = db_connection.get_all_audit_logs()
        assert len(logs) >= 1


class TestSettings:
    """Tests for settings management."""

    @pytest.mark.db
    @pytest.mark.unit
    def test_get_search_settings_defaults(self, db_connection):
        """Test getting default search settings."""
        settings = db_connection.get_search_settings()
        assert "searchMethod" in settings
        assert "limit" in settings
        assert "alpha" in settings

    @pytest.mark.db
    @pytest.mark.unit
    def test_update_search_settings(self, db_connection):
        """Test updating search settings."""
        db_connection.update_search_settings({"limit": 25})
        settings = db_connection.get_search_settings()
        assert settings["limit"] == 25

    @pytest.mark.db
    @pytest.mark.unit
    def test_settings_audit_log(self, db_connection):
        """Test that settings changes are logged."""
        db_connection.update_search_settings({"limit": 50})
        logs = db_connection.get_settings_audit_logs()
        assert len(logs) >= 1
