"""
Pytest configuration and fixtures for the document ingestion pipeline.

Provides fixtures for:
- SQLite database (in-memory for isolation)
- FastAPI TestClient
- Mock Temporal client
- Mock MinIO client
- Sample test data
"""

import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Set test environment before imports
os.environ["DOCUMENT_DB_PATH"] = ":memory:"
os.environ["MINIO_ACCESS_KEY"] = "test-access-key"
os.environ["MINIO_SECRET_KEY"] = "test-secret-key"
os.environ["TRANSLATION_VLLM_BASE_URL"] = "http://localhost:8000/v1"
os.environ["AUTH_DISABLED"] = "true"


@pytest.fixture(scope="function")
def temp_db_path(tmp_path):
    """Create a temporary database path for each test."""
    db_path = tmp_path / "test.db"
    os.environ["DOCUMENT_DB_PATH"] = str(db_path)
    yield str(db_path)
    # Cleanup happens automatically with tmp_path


@pytest.fixture(scope="function")
def db_connection(temp_db_path):
    """Initialize database and provide connection for testing."""
    from pipeline import db
    db.DB_PATH = temp_db_path
    db.init_db()
    yield db
    # Reset for next test
    db.DB_PATH = os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db")


@pytest.fixture
def mock_temporal_client():
    """Mock Temporal client for testing without actual Temporal server."""
    client = AsyncMock()

    # Mock workflow handle
    handle = AsyncMock()
    handle.query = AsyncMock(return_value={
        "stage": "registered",
        "document_id": "test-doc-id",
        "filename": "test.pdf",
        "page_count": 0,
        "chunk_count": 0,
        "error_message": None
    })
    handle.signal = AsyncMock()
    handle.cancel = AsyncMock()

    client.get_workflow_handle = MagicMock(return_value=handle)
    client.start_workflow = AsyncMock(return_value=handle)

    return client


@pytest.fixture
def mock_minio_client():
    """Mock MinIO client for testing without actual MinIO server."""
    client = MagicMock()
    client.bucket_exists = MagicMock(return_value=True)
    client.make_bucket = MagicMock()
    client.put_object = MagicMock()
    client.get_object = MagicMock(return_value=MagicMock(read=lambda: b"%PDF-test"))
    return client


@pytest.fixture
def test_client(mock_temporal_client, mock_minio_client):
    """Create FastAPI TestClient with mocked dependencies."""
    from fastapi.testclient import TestClient
    from pipeline import api

    # Patch the global clients
    api.temporal_client = mock_temporal_client
    api.minio_client = mock_minio_client

    # Initialize database
    api.db.init_db()

    with TestClient(api.app) as client:
        yield client


@pytest.fixture
def sample_pdf_content():
    """Sample PDF content for upload tests."""
    # Minimal valid PDF structure
    return b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>
endobj
xref
0 4
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
trailer
<< /Size 4 /Root 1 0 R >>
startxref
196
%%EOF"""


@pytest.fixture
def sample_page_data():
    """Sample page data for testing."""
    return {
        "page_number": 1,
        "original_markdown": "# Test Document\n\nThis is test content.",
        "edited_markdown": None,
        "detected_language": "en",
        "is_reviewed": False,
        "reviewer_notes": None
    }


@pytest.fixture
def sample_chunk_data():
    """Sample chunk data for testing."""
    return {
        "chunk_number": 1,
        "original_text": "This is a test chunk with some content.",
        "edited_text": None,
        "source_pages": [1],
        "token_count": 10,
        "is_reviewed": False,
        "is_excluded": False,
        "reviewer_notes": None
    }


@pytest.fixture
def sample_document(db_connection):
    """Create a sample document in the database."""
    workflow_id = "test-workflow-123"
    document_id = "test-doc-456"

    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id=document_id,
        filename="test.pdf",
        filepath="/app/books/test.pdf",
        stage="registered"
    )

    return {
        "workflow_id": workflow_id,
        "document_id": document_id,
        "filename": "test.pdf",
        "filepath": "/app/books/test.pdf",
        "stage": "registered"
    }


@pytest.fixture
def temp_pdf_file(tmp_path, sample_pdf_content):
    """Create a temporary PDF file for testing."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(sample_pdf_content)
    return pdf_path


# Markers for test categorization
def pytest_configure(config):
    """Configure custom pytest markers."""
    config.addinivalue_line("markers", "unit: Unit tests")
    config.addinivalue_line("markers", "integration: Integration tests")
    config.addinivalue_line("markers", "slow: Slow tests")
    config.addinivalue_line("markers", "api: API endpoint tests")
    config.addinivalue_line("markers", "db: Database tests")
    config.addinivalue_line("markers", "workflow: Temporal workflow tests")
