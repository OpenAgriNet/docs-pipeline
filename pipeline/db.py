"""
SQLite state persistence for document visibility.

This provides a fallback when Temporal workflow queries fail during
long-running activities, ensuring the dashboard always shows document status.
"""

import sqlite3
import os
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager
from typing import Optional
from threading import Lock

from .models import DocumentStage

# Database path - can be configured via environment
DB_PATH = os.environ.get("DOCUMENT_DB_PATH", "/data/documents.db")

# Lock for thread-safe operations
_db_lock = Lock()


def _ensure_db_dir():
    """Ensure the database directory exists."""
    db_dir = Path(DB_PATH).parent
    if not db_dir.exists():
        db_dir.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection():
    """Get a database connection with proper cleanup."""
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Initialize the database schema."""
    with _db_lock:
        with get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    workflow_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'registered',
                    page_count INTEGER DEFAULT 0,
                    chunk_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    ocr_completed_at TEXT,
                    chunks_completed_at TEXT,
                    ingested_at TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_documents_stage
                ON documents(stage)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_documents_created
                ON documents(created_at DESC)
            """)
            conn.commit()


def upsert_document(
    workflow_id: str,
    document_id: str,
    filename: str,
    filepath: str,
    stage: str = "registered",
    page_count: int = 0,
    chunk_count: int = 0,
    error_message: Optional[str] = None,
    ocr_completed_at: Optional[str] = None,
    chunks_completed_at: Optional[str] = None,
    ingested_at: Optional[str] = None
):
    """Insert or update a document record."""
    now = datetime.utcnow().isoformat()

    with _db_lock:
        with get_connection() as conn:
            # Check if exists
            row = conn.execute(
                "SELECT created_at FROM documents WHERE workflow_id = ?",
                (workflow_id,)
            ).fetchone()

            if row:
                # Update existing
                conn.execute("""
                    UPDATE documents SET
                        document_id = ?,
                        filename = ?,
                        filepath = ?,
                        stage = ?,
                        page_count = ?,
                        chunk_count = ?,
                        error_message = ?,
                        updated_at = ?,
                        ocr_completed_at = COALESCE(?, ocr_completed_at),
                        chunks_completed_at = COALESCE(?, chunks_completed_at),
                        ingested_at = COALESCE(?, ingested_at)
                    WHERE workflow_id = ?
                """, (
                    document_id, filename, filepath, stage,
                    page_count, chunk_count, error_message, now,
                    ocr_completed_at, chunks_completed_at, ingested_at,
                    workflow_id
                ))
            else:
                # Insert new
                conn.execute("""
                    INSERT INTO documents (
                        workflow_id, document_id, filename, filepath,
                        stage, page_count, chunk_count, error_message,
                        created_at, updated_at,
                        ocr_completed_at, chunks_completed_at, ingested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    workflow_id, document_id, filename, filepath,
                    stage, page_count, chunk_count, error_message,
                    now, now,
                    ocr_completed_at, chunks_completed_at, ingested_at
                ))

            conn.commit()


def update_document_stage(
    workflow_id: str,
    stage: str,
    page_count: Optional[int] = None,
    chunk_count: Optional[int] = None,
    error_message: Optional[str] = None
):
    """Update just the stage and counts for a document."""
    now = datetime.utcnow().isoformat()

    with _db_lock:
        with get_connection() as conn:
            # Build dynamic update
            updates = ["stage = ?", "updated_at = ?"]
            values = [stage, now]

            if page_count is not None:
                updates.append("page_count = ?")
                values.append(page_count)

            if chunk_count is not None:
                updates.append("chunk_count = ?")
                values.append(chunk_count)

            if error_message is not None:
                updates.append("error_message = ?")
                values.append(error_message)

            # Set timestamp based on stage
            if stage == "ocr_review":
                updates.append("ocr_completed_at = ?")
                values.append(now)
            elif stage == "chunk_review":
                updates.append("chunks_completed_at = ?")
                values.append(now)
            elif stage == "completed":
                updates.append("ingested_at = ?")
                values.append(now)

            values.append(workflow_id)

            conn.execute(
                f"UPDATE documents SET {', '.join(updates)} WHERE workflow_id = ?",
                values
            )
            conn.commit()


def get_document(workflow_id: str) -> Optional[dict]:
    """Get a single document by workflow ID."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE workflow_id = ?",
            (workflow_id,)
        ).fetchone()

        return dict(row) if row else None


def list_documents(
    stage: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> list[dict]:
    """List documents with optional stage filter."""
    with get_connection() as conn:
        if stage:
            rows = conn.execute("""
                SELECT * FROM documents
                WHERE stage = ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (stage, limit, offset)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM documents
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()

        return [dict(row) for row in rows]


def delete_document(workflow_id: str):
    """Delete a document record."""
    with _db_lock:
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM documents WHERE workflow_id = ?",
                (workflow_id,)
            )
            conn.commit()


def get_document_count(stage: Optional[str] = None) -> int:
    """Get count of documents, optionally filtered by stage."""
    with get_connection() as conn:
        if stage:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM documents WHERE stage = ?",
                (stage,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as cnt FROM documents").fetchone()

        return row["cnt"] if row else 0
