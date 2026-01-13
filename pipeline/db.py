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
import json

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
            # Audit logs table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    entity_type TEXT,
                    entity_id INTEGER,
                    field_name TEXT,
                    old_value TEXT,
                    new_value TEXT,
                    metadata TEXT,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_audit_workflow
                ON audit_logs(workflow_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_audit_timestamp
                ON audit_logs(timestamp DESC)
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
            # Get previous state for audit logging
            row = conn.execute(
                "SELECT stage, document_id FROM documents WHERE workflow_id = ?",
                (workflow_id,)
            ).fetchone()
            old_stage = row["stage"] if row else None
            document_id = row["document_id"] if row else workflow_id

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

    # Log stage transition if stage changed (outside lock to avoid deadlock)
    if old_stage and old_stage != stage:
        log_audit(
            workflow_id=workflow_id,
            document_id=document_id,
            action_type="stage_change",
            field_name="stage",
            old_value=old_stage,
            new_value=stage,
            metadata={"page_count": page_count, "chunk_count": chunk_count}
        )


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


# =============================================================================
# Audit Log Functions
# =============================================================================

def log_audit(
    workflow_id: str,
    document_id: str,
    action_type: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    field_name: Optional[str] = None,
    old_value: Optional[str] = None,
    new_value: Optional[str] = None,
    metadata: Optional[dict] = None
) -> int:
    """
    Log an audit entry.

    Args:
        workflow_id: The Temporal workflow ID
        document_id: The document hash ID
        action_type: Type of action (stage_change, page_edit, chunk_edit, approval, reset)
        entity_type: Type of entity (page, chunk, document)
        entity_id: ID of the entity (page_number or chunk_number)
        field_name: Name of the field that changed
        old_value: Previous value (as string or JSON)
        new_value: New value (as string or JSON)
        metadata: Additional context as dict (will be JSON serialized)

    Returns:
        The ID of the created audit log entry
    """
    now = datetime.utcnow().isoformat()
    metadata_json = json.dumps(metadata) if metadata else None

    with _db_lock:
        with get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO audit_logs (
                    workflow_id, document_id, action_type,
                    entity_type, entity_id, field_name,
                    old_value, new_value, metadata, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                workflow_id, document_id, action_type,
                entity_type, entity_id, field_name,
                old_value, new_value, metadata_json, now
            ))
            conn.commit()
            return cursor.lastrowid


def get_audit_logs(
    workflow_id: str,
    action_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> list[dict]:
    """
    Get audit logs for a document.

    Args:
        workflow_id: The Temporal workflow ID
        action_type: Optional filter by action type
        limit: Maximum number of entries to return
        offset: Offset for pagination

    Returns:
        List of audit log entries as dicts
    """
    with get_connection() as conn:
        if action_type:
            rows = conn.execute("""
                SELECT * FROM audit_logs
                WHERE workflow_id = ? AND action_type = ?
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """, (workflow_id, action_type, limit, offset)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM audit_logs
                WHERE workflow_id = ?
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
            """, (workflow_id, limit, offset)).fetchall()

        return [dict(row) for row in rows]


def get_audit_log_count(workflow_id: str, action_type: Optional[str] = None) -> int:
    """Get count of audit logs for a document."""
    with get_connection() as conn:
        if action_type:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM audit_logs WHERE workflow_id = ? AND action_type = ?",
                (workflow_id, action_type)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM audit_logs WHERE workflow_id = ?",
                (workflow_id,)
            ).fetchone()

        return row["cnt"] if row else 0
