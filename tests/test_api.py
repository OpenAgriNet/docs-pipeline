"""
Unit tests for pipeline/api.py - FastAPI endpoints.

Tests cover:
- Health endpoints
- Document listing and retrieval
- Page/chunk operations
- Approval workflows
- Settings endpoints
- Error handling
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
import os

# Set test environment
os.environ["DOCUMENT_DB_PATH"] = ":memory:"
os.environ["MINIO_ACCESS_KEY"] = "test-key"
os.environ["MINIO_SECRET_KEY"] = "test-secret"
os.environ["ALLOWED_FILE_PATHS"] = "/app/books,/tmp"


class TestHealthEndpoints:
    """Tests for health check endpoints."""

    @pytest.mark.api
    @pytest.mark.unit
    def test_health_endpoint(self, test_client):
        """Test health check returns ok status."""
        response = test_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    @pytest.mark.api
    @pytest.mark.unit
    def test_pipeline_stages_endpoint(self, test_client):
        """Test pipeline stages endpoint returns stage definitions."""
        response = test_client.get("/pipeline/stages")
        assert response.status_code == 200
        stages = response.json()
        assert len(stages) > 0
        assert all("id" in s and "label" in s for s in stages)


class TestDocumentEndpoints:
    """Tests for document CRUD endpoints."""

    @pytest.mark.api
    @pytest.mark.unit
    def test_list_documents(self, test_client, db_connection):
        """Test listing documents."""
        # Create a test document
        db_connection.upsert_document(
            workflow_id="api-test-001",
            document_id="doc-001",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="completed"
        )

        response = test_client.get("/documents")
        assert response.status_code == 200
        docs = response.json()
        assert isinstance(docs, list)

    @pytest.mark.api
    @pytest.mark.unit
    def test_list_documents_with_stage_filter(self, test_client, db_connection):
        """Test filtering documents by stage."""
        db_connection.upsert_document(
            workflow_id="filter-api-001",
            document_id="doc-filter-001",
            filename="completed.pdf",
            filepath="/app/books/completed.pdf",
            stage="completed"
        )

        response = test_client.get("/documents?stage=completed")
        assert response.status_code == 200
        docs = response.json()
        for doc in docs:
            assert doc["stage"] == "completed"

    @pytest.mark.api
    @pytest.mark.unit
    def test_get_document_from_db(self, test_client, db_connection):
        """Test getting document details from SQLite fallback."""
        workflow_id = "get-test-001"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-get-001",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="completed"
        )

        response = test_client.get(f"/documents/{workflow_id}")
        assert response.status_code == 200
        doc = response.json()
        assert doc["workflow_id"] == workflow_id

    @pytest.mark.api
    @pytest.mark.unit
    def test_get_document_not_found(self, test_client):
        """Test 404 for non-existent document."""
        response = test_client.get("/documents/nonexistent-workflow")
        assert response.status_code == 404


class TestPageEndpoints:
    """Tests for page operations."""

    @pytest.mark.api
    @pytest.mark.unit
    def test_list_pages(self, test_client, db_connection):
        """Test listing pages for a document."""
        workflow_id = "pages-test-001"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-pages",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="completed"
        )
        db_connection.persist_document_content(
            workflow_id=workflow_id,
            pages=[
                {"page_number": 1, "original_markdown": "Page 1", "detected_language": "en"},
                {"page_number": 2, "original_markdown": "Page 2", "detected_language": "en"}
            ],
            chunks=[]
        )

        response = test_client.get(f"/documents/{workflow_id}/pages")
        assert response.status_code == 200
        pages = response.json()
        assert len(pages) == 2

    @pytest.mark.api
    @pytest.mark.unit
    def test_get_page(self, test_client, db_connection):
        """Test getting a specific page."""
        workflow_id = "page-get-001"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-page",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="completed"
        )
        db_connection.persist_document_content(
            workflow_id=workflow_id,
            pages=[{"page_number": 1, "original_markdown": "Content", "detected_language": "en"}],
            chunks=[]
        )

        response = test_client.get(f"/documents/{workflow_id}/pages/1")
        assert response.status_code == 200
        page = response.json()
        assert page["page_number"] == 1

    @pytest.mark.api
    @pytest.mark.unit
    def test_get_page_invalid_number(self, test_client, db_connection):
        """Test validation rejects invalid page numbers."""
        workflow_id = "page-invalid-001"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-invalid",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="completed"
        )

        # Page 0 should be invalid (pages are 1-indexed)
        response = test_client.get(f"/documents/{workflow_id}/pages/0")
        assert response.status_code == 422  # Validation error

        # Negative page number
        response = test_client.get(f"/documents/{workflow_id}/pages/-1")
        assert response.status_code == 422

    @pytest.mark.api
    @pytest.mark.unit
    def test_update_page(self, test_client, db_connection):
        """Test updating a page."""
        workflow_id = "page-update-001"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-update",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="completed"
        )
        db_connection.persist_document_content(
            workflow_id=workflow_id,
            pages=[{"page_number": 1, "original_markdown": "Original", "detected_language": "en"}],
            chunks=[]
        )

        response = test_client.patch(
            f"/documents/{workflow_id}/pages/1",
            json={"edited_markdown": "Updated content"}
        )
        assert response.status_code == 200
        page = response.json()
        assert page["edited_markdown"] == "Updated content"


class TestChunkEndpoints:
    """Tests for chunk operations."""

    @pytest.mark.api
    @pytest.mark.unit
    def test_list_chunks(self, test_client, db_connection):
        """Test listing chunks for a document."""
        workflow_id = "chunks-test-001"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-chunks",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="completed"
        )
        db_connection.persist_document_content(
            workflow_id=workflow_id,
            pages=[],
            chunks=[
                {"chunk_number": 1, "original_text": "Chunk 1", "source_pages": [1], "token_count": 5},
                {"chunk_number": 2, "original_text": "Chunk 2", "source_pages": [1], "token_count": 5}
            ]
        )

        response = test_client.get(f"/documents/{workflow_id}/chunks")
        assert response.status_code == 200
        chunks = response.json()
        assert len(chunks) == 2

    @pytest.mark.api
    @pytest.mark.unit
    def test_list_chunks_include_excluded(self, test_client, db_connection):
        """Test including excluded chunks in listing."""
        workflow_id = "chunks-excluded-001"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-excluded",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="completed"
        )
        db_connection.persist_document_content(
            workflow_id=workflow_id,
            pages=[],
            chunks=[
                {"chunk_number": 1, "original_text": "Included", "source_pages": [1], "token_count": 5},
                {"chunk_number": 2, "original_text": "Excluded", "source_pages": [1], "token_count": 5}
            ]
        )
        db_connection.update_chunk(workflow_id, 2, is_excluded=True)

        # Without flag - should exclude
        response = test_client.get(f"/documents/{workflow_id}/chunks")
        assert len(response.json()) == 1

        # With flag - should include
        response = test_client.get(f"/documents/{workflow_id}/chunks?include_excluded=true")
        assert len(response.json()) == 2

    @pytest.mark.api
    @pytest.mark.unit
    def test_get_chunk_invalid_number(self, test_client, db_connection):
        """Test validation rejects invalid chunk numbers."""
        workflow_id = "chunk-invalid-001"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-chunk-invalid",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="completed"
        )

        response = test_client.get(f"/documents/{workflow_id}/chunks/0")
        assert response.status_code == 422


class TestAuditEndpoints:
    """Tests for audit log endpoints."""

    @pytest.mark.api
    @pytest.mark.unit
    def test_get_document_audit(self, test_client, db_connection):
        """Test getting audit logs for a document."""
        workflow_id = "audit-test-001"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-audit",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="registered"
        )
        db_connection.log_audit(
            workflow_id=workflow_id,
            document_id="doc-audit",
            action_type="test_action"
        )

        response = test_client.get(f"/documents/{workflow_id}/audit")
        assert response.status_code == 200
        data = response.json()
        assert "logs" in data
        assert "total" in data

    @pytest.mark.api
    @pytest.mark.unit
    def test_get_global_audit(self, test_client, db_connection):
        """Test getting global audit logs."""
        response = test_client.get("/audit")
        assert response.status_code == 200
        data = response.json()
        assert "logs" in data
        assert "total" in data

    @pytest.mark.api
    @pytest.mark.unit
    def test_audit_pagination(self, test_client, db_connection):
        """Test audit log pagination."""
        response = test_client.get("/audit?limit=5&offset=0")
        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 5
        assert data["offset"] == 0


class TestSettingsEndpoints:
    """Tests for settings endpoints."""

    @pytest.mark.api
    @pytest.mark.unit
    def test_get_search_settings(self, test_client, db_connection):
        """Test getting search settings."""
        response = test_client.get("/settings/search")
        assert response.status_code == 200
        settings = response.json()
        assert "searchMethod" in settings
        assert "limit" in settings

    @pytest.mark.api
    @pytest.mark.unit
    def test_update_search_settings(self, test_client, db_connection):
        """Test updating search settings."""
        response = test_client.put(
            "/settings/search",
            json={"limit": 25}
        )
        assert response.status_code == 200
        settings = response.json()
        assert settings["limit"] == 25

    @pytest.mark.api
    @pytest.mark.unit
    def test_get_settings_audit(self, test_client, db_connection):
        """Test getting settings audit trail."""
        # Make a change first
        test_client.put("/settings/search", json={"limit": 30})

        response = test_client.get("/settings/search/audit")
        assert response.status_code == 200
        data = response.json()
        assert "logs" in data


class TestErrorHandling:
    """Tests for error handling."""

    @pytest.mark.api
    @pytest.mark.unit
    def test_document_not_found(self, test_client):
        """Test 404 response for missing document."""
        response = test_client.get("/documents/nonexistent")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.api
    @pytest.mark.unit
    def test_page_not_found(self, test_client, db_connection):
        """Test 404 for missing page."""
        workflow_id = "page-404-test"
        db_connection.upsert_document(
            workflow_id=workflow_id,
            document_id="doc-404",
            filename="test.pdf",
            filepath="/app/books/test.pdf",
            stage="completed"
        )
        # No pages persisted

        response = test_client.get(f"/documents/{workflow_id}/pages/999")
        assert response.status_code == 404


class TestPdfHeaders:
    @pytest.mark.unit
    def test_inline_content_disposition_unicode_filename(self):
        from pipeline.api import _inline_content_disposition

        header = _inline_content_disposition("રબર મેટ.pdf")
        header.encode("latin-1")
        assert "filename*=" in header
        assert header.startswith('inline; filename="')

    @pytest.mark.unit
    def test_inline_content_disposition_strips_crlf(self):
        from pipeline.api import _inline_content_disposition

        header = _inline_content_disposition("evil.pdf\r\nX-Injected: yes")
        assert "\r" not in header
        assert "\n" not in header
        # Remains a single Content-Disposition value (no injected header line).
        assert header.count(":") >= 1
        assert "\r\nX-Injected" not in header
        assert header.startswith('inline; filename="')
