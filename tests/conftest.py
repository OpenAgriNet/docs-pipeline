"""Fixtures shared across the suite: a per-test SQLite database, the app client,
mocked Temporal/MinIO clients, the in-memory Marqo and recorded MinIO uploads."""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock

# Set test environment before imports
os.environ["DOCUMENT_DB_PATH"] = ":memory:"
os.environ["MINIO_ACCESS_KEY"] = "test-access-key"
os.environ["MINIO_SECRET_KEY"] = "test-secret-key"
os.environ["TRANSLATION_VLLM_BASE_URL"] = "http://localhost:8000/v1"
os.environ["AUTH_DISABLED"] = "true"
# A test that misses the Marqo fake fails instead of reaching a local Marqo.
os.environ["MARQO_URL"] = "http://marqo.invalid"
# Required at API/worker startup (no app-wide vendor default).
os.environ.setdefault("CHUNKING_PROVIDER", "deterministic")
os.environ.setdefault("CHUNKING_FALLBACK_PROVIDER", "deterministic")


@pytest.fixture
def temp_db_path(tmp_path, monkeypatch):
    """A fresh database file for this test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DOCUMENT_DB_PATH", db_path)
    return db_path


@pytest.fixture
def db_connection(temp_db_path, monkeypatch):
    """``pipeline.db`` bound to this test's own initialised database."""
    from pipeline import db

    monkeypatch.setattr(db, "DB_PATH", temp_db_path)
    db.init_db()
    return db


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
    client.remove_object = MagicMock()
    return client


@pytest.fixture
def test_client(db_connection, mock_temporal_client, mock_minio_client):
    """Create FastAPI TestClient with mocked dependencies."""
    from fastapi.testclient import TestClient
    from pipeline.app import app
    from pipeline.storage import minio as minio_storage
    from pipeline.temporal import client as temporal_client

    # Patch the global clients
    temporal_client._client = mock_temporal_client
    minio_storage._client = mock_minio_client

    with TestClient(app) as client:
        yield client

    temporal_client.reset()
    minio_storage.reset()


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
def fake_marqo(monkeypatch):
    """An empty in-memory Marqo bound for every production path (``tests.helpers.FakeMarqo``)."""
    from tests.helpers import FakeMarqo

    return FakeMarqo().install(monkeypatch)


@pytest.fixture
def minio_uploads(monkeypatch):
    """Stub ``document_tasks._upload_file_to_minio``; returns the recorded uploads.

    Each upload is ``{"workflow_id", "artifact_type", "filename", "instance"}``
    and gets a ``minio://documents/<wf>/<type>/<filename>`` URI back.
    """
    import mimetypes

    import pipeline.temporal.document_tasks as document_tasks

    uploads: list[dict] = []

    def _upload(local_path, workflow_id, artifact_type, filename, instance=None):
        uploads.append({
            "workflow_id": workflow_id,
            "artifact_type": artifact_type,
            "filename": filename,
            "instance": instance,
        })
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return (f"minio://documents/{workflow_id}/{artifact_type}/{filename}", 1, mime)

    monkeypatch.setattr(document_tasks, "_upload_file_to_minio", _upload)
    return uploads


@pytest.fixture
def temp_pdf_file(tmp_path, sample_pdf_content):
    """Create a temporary PDF file for testing."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(sample_pdf_content)
    return pdf_path

