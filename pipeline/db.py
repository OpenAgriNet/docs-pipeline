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


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Best-effort SQLite migration helper."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError:
        pass


def _ensure_db_dir():
    """Ensure the database directory exists."""
    db_dir = Path(DB_PATH).parent
    if not db_dir.exists():
        db_dir.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_connection():
    """Get a database connection with proper cleanup and isolation."""
    _ensure_db_dir()
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        isolation_level="IMMEDIATE",  # Acquire lock immediately on write
        timeout=30.0  # Wait up to 30s for locks
    )
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrent access
    conn.execute("PRAGMA journal_mode=WAL")
    # Ensure foreign keys are enforced
    conn.execute("PRAGMA foreign_keys=ON")
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
                    canonical_document_id TEXT,
                    filename TEXT NOT NULL,
                    display_name TEXT,
                    source_filename TEXT,
                    source_manifest_name TEXT,
                    source_file_fingerprint TEXT,
                    filepath TEXT NOT NULL,
                    stage TEXT NOT NULL DEFAULT 'registered',
                    page_count INTEGER DEFAULT 0,
                    chunk_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    is_demo INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT,
                    ocr_completed_at TEXT,
                    translation_completed_at TEXT,
                    chunks_completed_at TEXT,
                    ingested_at TEXT,
                    source_type TEXT,
                    canonical_input_type TEXT,
                    stop_after_ocr INTEGER DEFAULT 0,
                    original_artifact_id INTEGER,
                    normalized_artifact_id INTEGER,
                    latest_job_id INTEGER
                )
            """)
            _add_column_if_missing(conn, "documents", "is_demo", "INTEGER DEFAULT 0")
            _add_column_if_missing(conn, "documents", "is_disabled", "INTEGER DEFAULT 0")
            _add_column_if_missing(conn, "documents", "display_name", "TEXT")
            _add_column_if_missing(conn, "documents", "canonical_document_id", "TEXT")
            _add_column_if_missing(conn, "documents", "source_filename", "TEXT")
            _add_column_if_missing(conn, "documents", "source_manifest_name", "TEXT")
            _add_column_if_missing(conn, "documents", "source_file_fingerprint", "TEXT")
            _add_column_if_missing(conn, "documents", "translation_completed_at", "TEXT")
            _add_column_if_missing(conn, "documents", "source_type", "TEXT")
            _add_column_if_missing(conn, "documents", "canonical_input_type", "TEXT")
            _add_column_if_missing(conn, "documents", "stop_after_ocr", "INTEGER DEFAULT 0")
            _add_column_if_missing(conn, "documents", "original_artifact_id", "INTEGER")
            _add_column_if_missing(conn, "documents", "normalized_artifact_id", "INTEGER")
            _add_column_if_missing(conn, "documents", "latest_job_id", "INTEGER")
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
            # Pages table for persistence after workflow completion
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    page_number INTEGER NOT NULL,
                    original_markdown TEXT,
                    edited_markdown TEXT,
                    is_reviewed INTEGER DEFAULT 0,
                    reviewer_notes TEXT,
                    detected_language TEXT,
                    translated_markdown TEXT,
                    edited_translation TEXT,
                    translation_reviewed INTEGER DEFAULT 0,
                    translation_notes TEXT,
                    translation_provider TEXT,
                    translation_model TEXT,
                    translation_target_language TEXT,
                    translated_at TEXT,
                    UNIQUE(workflow_id, page_number)
                )
            """)
            _add_column_if_missing(conn, "pages", "translation_provider", "TEXT")
            _add_column_if_missing(conn, "pages", "translation_model", "TEXT")
            _add_column_if_missing(conn, "pages", "translation_target_language", "TEXT")
            _add_column_if_missing(conn, "pages", "translated_at", "TEXT")
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pages_workflow
                ON pages(workflow_id)
            """)
            # Chunks table for persistence after workflow completion
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    chunk_number INTEGER NOT NULL,
                    original_text TEXT,
                    edited_text TEXT,
                    token_count INTEGER DEFAULT 0,
                    page_start INTEGER DEFAULT 1,
                    page_end INTEGER DEFAULT 1,
                    is_reviewed INTEGER DEFAULT 0,
                    is_excluded INTEGER DEFAULT 0,
                    reviewer_notes TEXT,
                    UNIQUE(workflow_id, chunk_number)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_chunks_workflow
                ON chunks(workflow_id)
            """)
            # Settings table for application configuration
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    description TEXT,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS document_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    temporal_workflow_id TEXT,
                    temporal_run_id TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    current_stage TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error_message TEXT,
                    config_json TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_document_jobs_workflow
                ON document_jobs(workflow_id, started_at DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS document_artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL,
                    job_id INTEGER,
                    artifact_type TEXT NOT NULL,
                    stage TEXT,
                    storage_uri TEXT NOT NULL,
                    mime_type TEXT,
                    filename TEXT,
                    size_bytes INTEGER,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_document_artifacts_workflow
                ON document_artifacts(workflow_id, created_at DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS document_index_status (
                    workflow_id TEXT NOT NULL,
                    index_name TEXT NOT NULL,
                    marqo_doc_id TEXT,
                    chunk_count_indexed INTEGER DEFAULT 0,
                    last_indexed_at TEXT,
                    last_verified_at TEXT,
                    schema_version TEXT,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    details_json TEXT,
                    PRIMARY KEY (workflow_id, index_name)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS document_manifest_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    manifest_filename TEXT NOT NULL UNIQUE,
                    title_en TEXT,
                    title_gu TEXT,
                    doc_language TEXT,
                    category_tags TEXT,
                    description TEXT,
                    quality_score TEXT,
                    priority_rank TEXT,
                    ingestion_status TEXT,
                    feedback TEXT,
                    source_csv_path TEXT,
                    imported_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_manifest_entries_filename
                ON document_manifest_entries(manifest_filename)
            """)
            # Insert default search settings if not exists
            default_settings = [
                ("search_method", "HYBRID", "Search method: TENSOR, LEXICAL, or HYBRID"),
                ("search_limit", "12", "Number of search results to return"),
                ("search_alpha", "0.6", "Hybrid search alpha: 0=lexical, 1=semantic"),
                ("search_ranking_method", "rrf", "Hybrid ranking: rrf or normalize_linear"),
                ("search_show_highlights", "true", "Show highlighted matches in results"),
                ("search_ef_search", "256", "HNSW search accuracy parameter"),
                ("search_index_name", "documents-index", "Default Marqo index name"),
                ("search_candidate_cap", "120", "Candidate retrieval pool cap"),
                ("search_candidate_multiplier", "10", "Candidate pool multiplier before final cut"),
                ("search_max_chunks_per_doc", "2", "Final result diversity cap per document"),
                ("search_use_e5_prefix", "true", "Prefix search queries with e5 query:"),
                ("search_exclude_reference", "true", "Exclude reference chunks when index supports it"),
                ("search_query_expansion_profile", "gu-v1", "Query expansion profile"),
                ("search_rerank_mode", "none", "Post-search reranking mode"),
                ("search_hybrid_rrfk", "60", "RRF tuning parameter for hybrid search"),
            ]
            for key, value, description in default_settings:
                conn.execute("""
                    INSERT OR IGNORE INTO settings (key, value, description, updated_at)
                    VALUES (?, ?, ?, ?)
                """, (key, value, description, datetime.utcnow().isoformat()))
            conn.commit()


def upsert_document(
    workflow_id: str,
    document_id: str,
    filename: str,
    filepath: str,
    canonical_document_id: Optional[str] = None,
    display_name: Optional[str] = None,
    source_filename: Optional[str] = None,
    source_manifest_name: Optional[str] = None,
    source_file_fingerprint: Optional[str] = None,
    stage: str = "registered",
    page_count: int = 0,
    chunk_count: int = 0,
    error_message: Optional[str] = None,
    ocr_completed_at: Optional[str] = None,
    translation_completed_at: Optional[str] = None,
    chunks_completed_at: Optional[str] = None,
    ingested_at: Optional[str] = None,
    source_type: Optional[str] = None,
    canonical_input_type: Optional[str] = None,
    stop_after_ocr: bool = False,
    original_artifact_id: Optional[int] = None,
    normalized_artifact_id: Optional[int] = None,
    latest_job_id: Optional[int] = None,
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
                        canonical_document_id = COALESCE(?, canonical_document_id),
                        filename = ?,
                        display_name = COALESCE(?, display_name),
                        source_filename = COALESCE(?, source_filename),
                        source_manifest_name = COALESCE(?, source_manifest_name),
                        source_file_fingerprint = COALESCE(?, source_file_fingerprint),
                        filepath = ?,
                        stage = ?,
                        page_count = ?,
                        chunk_count = ?,
                        error_message = ?,
                        updated_at = ?,
                        ocr_completed_at = COALESCE(?, ocr_completed_at),
                        translation_completed_at = COALESCE(?, translation_completed_at),
                        chunks_completed_at = COALESCE(?, chunks_completed_at),
                        ingested_at = COALESCE(?, ingested_at),
                        source_type = COALESCE(?, source_type),
                        canonical_input_type = COALESCE(?, canonical_input_type),
                        stop_after_ocr = COALESCE(?, stop_after_ocr),
                        original_artifact_id = COALESCE(?, original_artifact_id),
                        normalized_artifact_id = COALESCE(?, normalized_artifact_id),
                        latest_job_id = COALESCE(?, latest_job_id)
                    WHERE workflow_id = ?
                """, (
                    document_id, canonical_document_id, filename, display_name,
                    source_filename, source_manifest_name, source_file_fingerprint,
                    filepath, stage,
                    page_count, chunk_count, error_message, now,
                    ocr_completed_at, translation_completed_at, chunks_completed_at, ingested_at,
                    source_type, canonical_input_type, 1 if stop_after_ocr else 0,
                    original_artifact_id, normalized_artifact_id, latest_job_id,
                    workflow_id
                ))
            else:
                # Insert new
                conn.execute("""
                    INSERT INTO documents (
                        workflow_id, document_id, filename, filepath,
                        canonical_document_id, display_name, source_filename, source_manifest_name,
                        source_file_fingerprint,
                        stage, page_count, chunk_count, error_message,
                        created_at, updated_at,
                        ocr_completed_at, translation_completed_at, chunks_completed_at, ingested_at,
                        source_type, canonical_input_type, stop_after_ocr,
                        original_artifact_id, normalized_artifact_id, latest_job_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    workflow_id, document_id, filename, filepath,
                    canonical_document_id, display_name, source_filename, source_manifest_name,
                    source_file_fingerprint,
                    stage, page_count, chunk_count, error_message,
                    now, now,
                    ocr_completed_at, translation_completed_at, chunks_completed_at, ingested_at,
                    source_type, canonical_input_type, 1 if stop_after_ocr else 0,
                    original_artifact_id, normalized_artifact_id, latest_job_id
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
            elif stage == "translation_review":
                updates.append("translation_completed_at = ?")
                values.append(now)
            elif stage == "chunk_review":
                updates.append("chunks_completed_at = ?")
                values.append(now)
            elif stage == "completed":
                updates.append("ingested_at = ?")
                values.append(now)

            values.append(workflow_id)

            # Note: updates list contains only hardcoded field names, not user input
            # Values are properly parameterized via ?
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
    offset: int = 0,
    include_demo: bool = False,
    include_disabled: bool = False
) -> list[dict]:
    """List documents with optional stage filter.

    Args:
        stage: Filter by document stage
        limit: Max documents to return
        offset: Pagination offset
        include_demo: If False (default), excludes demo documents from results
        include_disabled: If False (default), excludes soft-deleted documents from results
    """
    with get_connection() as conn:
        # Note: filters are hardcoded SQL fragments based on boolean flags, not user input
        demo_filter = "" if include_demo else "AND (is_demo = 0 OR is_demo IS NULL)"
        disabled_filter = "" if include_disabled else "AND (is_disabled = 0 OR is_disabled IS NULL)"

        if stage:
            rows = conn.execute(f"""
                SELECT * FROM documents
                WHERE stage = ? {demo_filter} {disabled_filter}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (stage, limit, offset)).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT * FROM documents
                WHERE 1=1 {demo_filter} {disabled_filter}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()

        return [dict(row) for row in rows]


def set_document_demo(workflow_id: str, is_demo: bool = True):
    """Mark a document as demo (filtered from UI by default)."""
    with _db_lock:
        with get_connection() as conn:
            conn.execute(
                "UPDATE documents SET is_demo = ? WHERE workflow_id = ?",
                (1 if is_demo else 0, workflow_id)
            )
            conn.commit()


def set_document_disabled(workflow_id: str, is_disabled: bool = True):
    """Soft delete a document (filtered from all queries by default).

    Unlike hard delete, this preserves the document record for audit purposes.
    Use X-Include-Disabled: true header in API to see disabled documents.
    """
    with _db_lock:
        with get_connection() as conn:
            conn.execute(
                "UPDATE documents SET is_disabled = ?, updated_at = ? WHERE workflow_id = ?",
                (1 if is_disabled else 0, datetime.utcnow().isoformat(), workflow_id)
            )
            conn.commit()


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


def upsert_manifest_entry(
    manifest_filename: str,
    title_en: Optional[str] = None,
    title_gu: Optional[str] = None,
    doc_language: Optional[str] = None,
    category_tags: Optional[str] = None,
    description: Optional[str] = None,
    quality_score: Optional[str] = None,
    priority_rank: Optional[str] = None,
    ingestion_status: Optional[str] = None,
    feedback: Optional[str] = None,
    source_csv_path: Optional[str] = None,
) -> dict:
    imported_at = datetime.utcnow().isoformat()
    with _db_lock:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO document_manifest_entries (
                    manifest_filename, title_en, title_gu, doc_language,
                    category_tags, description, quality_score, priority_rank,
                    ingestion_status, feedback, source_csv_path, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(manifest_filename) DO UPDATE SET
                    title_en = COALESCE(excluded.title_en, document_manifest_entries.title_en),
                    title_gu = COALESCE(excluded.title_gu, document_manifest_entries.title_gu),
                    doc_language = COALESCE(excluded.doc_language, document_manifest_entries.doc_language),
                    category_tags = COALESCE(excluded.category_tags, document_manifest_entries.category_tags),
                    description = COALESCE(excluded.description, document_manifest_entries.description),
                    quality_score = COALESCE(excluded.quality_score, document_manifest_entries.quality_score),
                    priority_rank = COALESCE(excluded.priority_rank, document_manifest_entries.priority_rank),
                    ingestion_status = COALESCE(excluded.ingestion_status, document_manifest_entries.ingestion_status),
                    feedback = COALESCE(excluded.feedback, document_manifest_entries.feedback),
                    source_csv_path = COALESCE(excluded.source_csv_path, document_manifest_entries.source_csv_path),
                    imported_at = excluded.imported_at
            """, (
                manifest_filename, title_en, title_gu, doc_language,
                category_tags, description, quality_score, priority_rank,
                ingestion_status, feedback, source_csv_path, imported_at
            ))
            conn.commit()
    return get_manifest_entry(manifest_filename) or {}


def get_manifest_entry(manifest_filename: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM document_manifest_entries WHERE manifest_filename = ?",
            (manifest_filename,),
        ).fetchone()
        return dict(row) if row else None


def list_manifest_entries(limit: int = 1000, offset: int = 0) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM document_manifest_entries
            ORDER BY manifest_filename
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
        return [dict(row) for row in rows]


def update_document_fields(workflow_id: str, **updates: object) -> Optional[dict]:
    """Update selected document fields and return the document."""
    if not updates:
        return get_document(workflow_id)

    allowed_fields = {
        "stage", "page_count", "chunk_count", "error_message", "updated_at",
        "ocr_completed_at", "translation_completed_at", "chunks_completed_at", "ingested_at",
        "source_type", "canonical_input_type", "original_artifact_id", "normalized_artifact_id",
        "latest_job_id", "filepath", "filename", "display_name", "document_id",
        "canonical_document_id", "source_filename", "source_manifest_name",
        "source_file_fingerprint", "stop_after_ocr",
    }
    set_clauses = []
    values: list[object] = []
    for key, value in updates.items():
        if key not in allowed_fields:
            continue
        set_clauses.append(f"{key} = ?")
        values.append(value)

    if not set_clauses:
        return get_document(workflow_id)

    if "updated_at" not in updates:
        set_clauses.append("updated_at = ?")
        values.append(datetime.utcnow().isoformat())

    values.append(workflow_id)
    with _db_lock:
        with get_connection() as conn:
            conn.execute(
                f"UPDATE documents SET {', '.join(set_clauses)} WHERE workflow_id = ?",
                values,
            )
            conn.commit()
    return get_document(workflow_id)


def create_document_job(
    workflow_id: str,
    job_type: str,
    temporal_workflow_id: Optional[str] = None,
    temporal_run_id: Optional[str] = None,
    status: str = "running",
    current_stage: Optional[str] = None,
    config: Optional[dict] = None,
) -> int:
    now = datetime.utcnow().isoformat()
    config_json = json.dumps(config) if config else None
    with _db_lock:
        with get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO document_jobs (
                    workflow_id, job_type, temporal_workflow_id, temporal_run_id,
                    status, current_stage, started_at, config_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                workflow_id, job_type, temporal_workflow_id, temporal_run_id,
                status, current_stage, now, config_json
            ))
            job_id = cursor.lastrowid
            conn.execute(
                "UPDATE documents SET latest_job_id = ?, updated_at = ? WHERE workflow_id = ?",
                (job_id, now, workflow_id),
            )
            conn.commit()
            return job_id


def update_document_job(job_id: int, **updates: object) -> Optional[dict]:
    if not updates:
        return get_document_job(job_id)
    allowed_fields = {
        "status", "current_stage", "completed_at", "error_message", "temporal_run_id", "config_json"
    }
    set_clauses = []
    values: list[object] = []
    for key, value in updates.items():
        if key not in allowed_fields:
            continue
        if key == "config_json" and isinstance(value, dict):
            value = json.dumps(value)
        set_clauses.append(f"{key} = ?")
        values.append(value)
    if not set_clauses:
        return get_document_job(job_id)
    values.append(job_id)
    with _db_lock:
        with get_connection() as conn:
            conn.execute(
                f"UPDATE document_jobs SET {', '.join(set_clauses)} WHERE id = ?",
                values,
            )
            conn.commit()
    return get_document_job(job_id)


def get_document_job(job_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM document_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None


def get_latest_document_job(workflow_id: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT * FROM document_jobs
            WHERE workflow_id = ?
            ORDER BY started_at DESC, id DESC
            LIMIT 1
        """, (workflow_id,)).fetchone()
        return dict(row) if row else None


def list_document_jobs(workflow_id: str, limit: int = 50) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM document_jobs
            WHERE workflow_id = ?
            ORDER BY started_at DESC, id DESC
            LIMIT ?
        """, (workflow_id, limit)).fetchall()
        return [dict(row) for row in rows]


def add_document_artifact(
    workflow_id: str,
    artifact_type: str,
    storage_uri: str,
    stage: Optional[str] = None,
    job_id: Optional[int] = None,
    mime_type: Optional[str] = None,
    filename: Optional[str] = None,
    size_bytes: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> int:
    created_at = datetime.utcnow().isoformat()
    metadata_json = json.dumps(metadata) if metadata else None
    with _db_lock:
        with get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO document_artifacts (
                    workflow_id, job_id, artifact_type, stage, storage_uri,
                    mime_type, filename, size_bytes, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                workflow_id, job_id, artifact_type, stage, storage_uri,
                mime_type, filename, size_bytes, metadata_json, created_at
            ))
            artifact_id = cursor.lastrowid
            if artifact_type == "original_upload":
                conn.execute("UPDATE documents SET original_artifact_id = ? WHERE workflow_id = ?", (artifact_id, workflow_id))
            elif artifact_type in {"normalized_pdf", "normalized_spreadsheet"}:
                conn.execute("UPDATE documents SET normalized_artifact_id = ? WHERE workflow_id = ?", (artifact_id, workflow_id))
            conn.commit()
            return artifact_id


def list_document_artifacts(workflow_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM document_artifacts
            WHERE workflow_id = ?
            ORDER BY created_at DESC, id DESC
        """, (workflow_id,)).fetchall()
        return [dict(row) for row in rows]


def get_document_artifact(workflow_id: str, artifact_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT * FROM document_artifacts
            WHERE workflow_id = ? AND id = ?
        """, (workflow_id, artifact_id)).fetchone()
        return dict(row) if row else None


def upsert_document_index_status(
    workflow_id: str,
    index_name: str,
    marqo_doc_id: Optional[str] = None,
    chunk_count_indexed: Optional[int] = None,
    last_indexed_at: Optional[str] = None,
    last_verified_at: Optional[str] = None,
    schema_version: Optional[str] = None,
    status: str = "unknown",
    details: Optional[dict] = None,
) -> dict:
    details_json = json.dumps(details) if details else None
    with _db_lock:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO document_index_status (
                    workflow_id, index_name, marqo_doc_id, chunk_count_indexed,
                    last_indexed_at, last_verified_at, schema_version, status, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, index_name) DO UPDATE SET
                    marqo_doc_id = COALESCE(excluded.marqo_doc_id, document_index_status.marqo_doc_id),
                    chunk_count_indexed = COALESCE(excluded.chunk_count_indexed, document_index_status.chunk_count_indexed),
                    last_indexed_at = COALESCE(excluded.last_indexed_at, document_index_status.last_indexed_at),
                    last_verified_at = COALESCE(excluded.last_verified_at, document_index_status.last_verified_at),
                    schema_version = COALESCE(excluded.schema_version, document_index_status.schema_version),
                    status = excluded.status,
                    details_json = COALESCE(excluded.details_json, document_index_status.details_json)
            """, (
                workflow_id, index_name, marqo_doc_id, chunk_count_indexed,
                last_indexed_at, last_verified_at, schema_version, status, details_json
            ))
            conn.commit()
    return get_document_index_status(workflow_id, index_name) or {}


def get_document_index_status(workflow_id: str, index_name: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("""
            SELECT * FROM document_index_status
            WHERE workflow_id = ? AND index_name = ?
        """, (workflow_id, index_name)).fetchone()
        return dict(row) if row else None


def list_document_index_status(workflow_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM document_index_status
            WHERE workflow_id = ?
            ORDER BY last_verified_at DESC, last_indexed_at DESC
        """, (workflow_id,)).fetchall()
        return [dict(row) for row in rows]


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


def get_all_audit_logs(
    action_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
) -> list[dict]:
    """
    Get all audit logs across all documents.

    Args:
        action_type: Optional filter by action type
        limit: Maximum number of entries to return
        offset: Offset for pagination

    Returns:
        List of audit log entries as dicts, with document filename included
    """
    with get_connection() as conn:
        if action_type:
            rows = conn.execute("""
                SELECT a.*, d.filename
                FROM audit_logs a
                LEFT JOIN documents d ON a.workflow_id = d.workflow_id
                WHERE a.action_type = ?
                ORDER BY a.timestamp DESC
                LIMIT ? OFFSET ?
            """, (action_type, limit, offset)).fetchall()
        else:
            rows = conn.execute("""
                SELECT a.*, d.filename
                FROM audit_logs a
                LEFT JOIN documents d ON a.workflow_id = d.workflow_id
                ORDER BY a.timestamp DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()

        return [dict(row) for row in rows]


def get_all_audit_log_count(action_type: Optional[str] = None) -> int:
    """Get total count of all audit logs."""
    with get_connection() as conn:
        if action_type:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM audit_logs WHERE action_type = ?",
                (action_type,)
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as cnt FROM audit_logs").fetchone()

        return row["cnt"] if row else 0


# =============================================================================
# Page Functions (for persistence after workflow completion)
# =============================================================================

def save_pages(workflow_id: str, pages: list[dict]):
    """
    Save all pages for a document (bulk upsert).
    Called when workflow completes to persist data.
    """
    with _db_lock:
        with get_connection() as conn:
            for page in pages:
                conn.execute("""
                    INSERT OR REPLACE INTO pages (
                        workflow_id, page_number, original_markdown, edited_markdown,
                        is_reviewed, reviewer_notes, detected_language,
                        translated_markdown, edited_translation,
                        translation_reviewed, translation_notes,
                        translation_provider, translation_model,
                        translation_target_language, translated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    workflow_id,
                    page.get("page_number"),
                    page.get("original_markdown"),
                    page.get("edited_markdown"),
                    1 if page.get("is_reviewed") else 0,
                    page.get("reviewer_notes"),
                    page.get("detected_language"),
                    page.get("translated_markdown"),
                    page.get("edited_translation"),
                    1 if page.get("translation_reviewed") else 0,
                    page.get("translation_notes"),
                    page.get("translation_provider"),
                    page.get("translation_model"),
                    page.get("translation_target_language"),
                    page.get("translated_at"),
                ))
            conn.commit()


def persist_document_content(workflow_id: str, pages: list[dict], chunks: list[dict]):
    """Backward-compatible helper to persist pages and chunks in one call."""
    save_pages(workflow_id, pages)
    save_chunks(workflow_id, chunks)


def get_pages(workflow_id: str) -> list[dict]:
    """Get all pages for a document from SQLite."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM pages
            WHERE workflow_id = ?
            ORDER BY page_number
        """, (workflow_id,)).fetchall()

        pages = []
        for row in rows:
            page = dict(row)
            # Convert SQLite integers back to booleans
            page["is_reviewed"] = bool(page.get("is_reviewed"))
            page["translation_reviewed"] = bool(page.get("translation_reviewed"))
            pages.append(page)
        return pages


def get_page(workflow_id: str, page_num: int) -> Optional[dict]:
    """Get a specific page from SQLite."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT * FROM pages
            WHERE workflow_id = ? AND page_number = ?
        """, (workflow_id, page_num)).fetchone()

        if row:
            page = dict(row)
            page["is_reviewed"] = bool(page.get("is_reviewed"))
            page["translation_reviewed"] = bool(page.get("translation_reviewed"))
            return page
        return None


def update_page(
    workflow_id: str,
    page_num: int,
    edited_markdown: Optional[str] = None,
    is_reviewed: Optional[bool] = None,
    reviewer_notes: Optional[str] = None
) -> Optional[dict]:
    """Update a page in SQLite. Returns updated page or None if not found."""
    with _db_lock:
        with get_connection() as conn:
            # Check if page exists
            existing = conn.execute(
                "SELECT id FROM pages WHERE workflow_id = ? AND page_number = ?",
                (workflow_id, page_num)
            ).fetchone()

            if not existing:
                return None

            # Build dynamic update
            updates = []
            values = []

            if edited_markdown is not None:
                updates.append("edited_markdown = ?")
                values.append(edited_markdown)

            if is_reviewed is not None:
                updates.append("is_reviewed = ?")
                values.append(1 if is_reviewed else 0)

            if reviewer_notes is not None:
                updates.append("reviewer_notes = ?")
                values.append(reviewer_notes)

            if updates:
                values.extend([workflow_id, page_num])
                # Note: updates list contains only hardcoded field names, not user input
                conn.execute(
                    f"UPDATE pages SET {', '.join(updates)} WHERE workflow_id = ? AND page_number = ?",
                    values
                )
                conn.commit()

    return get_page(workflow_id, page_num)


def reset_page(workflow_id: str, page_num: int) -> Optional[dict]:
    """Reset a page to original markdown in SQLite."""
    with _db_lock:
        with get_connection() as conn:
            conn.execute("""
                UPDATE pages SET
                    edited_markdown = NULL,
                    is_reviewed = 0,
                    reviewer_notes = NULL
                WHERE workflow_id = ? AND page_number = ?
            """, (workflow_id, page_num))
            conn.commit()

    return get_page(workflow_id, page_num)


# =============================================================================
# Chunk Functions (for persistence after workflow completion)
# =============================================================================

def save_chunks(workflow_id: str, chunks: list[dict]):
    """
    Save all chunks for a document (bulk upsert).
    Called when workflow completes to persist data.
    """
    with _db_lock:
        with get_connection() as conn:
            for chunk in chunks:
                conn.execute("""
                    INSERT OR REPLACE INTO chunks (
                        workflow_id, chunk_number, original_text, edited_text,
                        token_count, page_start, page_end,
                        is_reviewed, is_excluded, reviewer_notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    workflow_id,
                    chunk.get("chunk_number"),
                    chunk.get("original_text"),
                    chunk.get("edited_text"),
                    chunk.get("token_count", 0),
                    chunk.get("page_start", 1),
                    chunk.get("page_end", 1),
                    1 if chunk.get("is_reviewed") else 0,
                    1 if chunk.get("is_excluded") else 0,
                    chunk.get("reviewer_notes")
                ))
            conn.commit()


def get_chunks(workflow_id: str, include_excluded: bool = False) -> list[dict]:
    """Get all chunks for a document from SQLite."""
    with get_connection() as conn:
        if include_excluded:
            rows = conn.execute("""
                SELECT * FROM chunks
                WHERE workflow_id = ?
                ORDER BY chunk_number
            """, (workflow_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM chunks
                WHERE workflow_id = ? AND is_excluded = 0
                ORDER BY chunk_number
            """, (workflow_id,)).fetchall()

        chunks = []
        for row in rows:
            chunk = dict(row)
            chunk["is_reviewed"] = bool(chunk.get("is_reviewed"))
            chunk["is_excluded"] = bool(chunk.get("is_excluded"))
            chunks.append(chunk)
        return chunks


def get_chunk(workflow_id: str, chunk_num: int) -> Optional[dict]:
    """Get a specific chunk from SQLite."""
    with get_connection() as conn:
        row = conn.execute("""
            SELECT * FROM chunks
            WHERE workflow_id = ? AND chunk_number = ?
        """, (workflow_id, chunk_num)).fetchone()

        if row:
            chunk = dict(row)
            chunk["is_reviewed"] = bool(chunk.get("is_reviewed"))
            chunk["is_excluded"] = bool(chunk.get("is_excluded"))
            return chunk
        return None


def update_chunk(
    workflow_id: str,
    chunk_num: int,
    edited_text: Optional[str] = None,
    is_reviewed: Optional[bool] = None,
    is_excluded: Optional[bool] = None,
    reviewer_notes: Optional[str] = None
) -> Optional[dict]:
    """Update a chunk in SQLite. Returns updated chunk or None if not found."""
    with _db_lock:
        with get_connection() as conn:
            # Check if chunk exists
            existing = conn.execute(
                "SELECT id FROM chunks WHERE workflow_id = ? AND chunk_number = ?",
                (workflow_id, chunk_num)
            ).fetchone()

            if not existing:
                return None

            # Build dynamic update
            updates = []
            values = []

            if edited_text is not None:
                updates.append("edited_text = ?")
                values.append(edited_text)

            if is_reviewed is not None:
                updates.append("is_reviewed = ?")
                values.append(1 if is_reviewed else 0)

            if is_excluded is not None:
                updates.append("is_excluded = ?")
                values.append(1 if is_excluded else 0)

            if reviewer_notes is not None:
                updates.append("reviewer_notes = ?")
                values.append(reviewer_notes)

            if updates:
                values.extend([workflow_id, chunk_num])
                # Note: updates list contains only hardcoded field names, not user input
                conn.execute(
                    f"UPDATE chunks SET {', '.join(updates)} WHERE workflow_id = ? AND chunk_number = ?",
                    values
                )
                conn.commit()

    return get_chunk(workflow_id, chunk_num)


def reset_chunk(workflow_id: str, chunk_num: int) -> Optional[dict]:
    """Reset a chunk to original text in SQLite."""
    with _db_lock:
        with get_connection() as conn:
            conn.execute("""
                UPDATE chunks SET
                    edited_text = NULL,
                    is_reviewed = 0,
                    is_excluded = 0,
                    reviewer_notes = NULL
                WHERE workflow_id = ? AND chunk_number = ?
            """, (workflow_id, chunk_num))
            conn.commit()

    return get_chunk(workflow_id, chunk_num)


# =============================================================================
# Settings Functions
# =============================================================================

def get_setting(key: str) -> Optional[dict]:
    """Get a single setting by key."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM settings WHERE key = ?",
            (key,)
        ).fetchone()
        return dict(row) if row else None


def get_all_settings() -> dict:
    """Get all settings as a dict of key -> value."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM settings").fetchall()
        return {row["key"]: dict(row) for row in rows}


def get_search_settings() -> dict:
    """Get all search-related settings as a simple dict."""
    all_settings = get_all_settings()
    return {
        "searchMethod": all_settings.get("search_method", {}).get("value", "HYBRID"),
        "limit": int(all_settings.get("search_limit", {}).get("value", "12")),
        "alpha": float(all_settings.get("search_alpha", {}).get("value", "0.6")),
        "rankingMethod": all_settings.get("search_ranking_method", {}).get("value", "rrf"),
        "showHighlights": all_settings.get("search_show_highlights", {}).get("value", "true") == "true",
        "efSearch": int(all_settings.get("search_ef_search", {}).get("value", "256")),
        "indexName": all_settings.get("search_index_name", {}).get("value", "documents-index"),
        "candidateCap": int(all_settings.get("search_candidate_cap", {}).get("value", "120")),
        "candidateMultiplier": int(all_settings.get("search_candidate_multiplier", {}).get("value", "10")),
        "maxChunksPerDoc": int(all_settings.get("search_max_chunks_per_doc", {}).get("value", "2")),
        "useE5Prefix": all_settings.get("search_use_e5_prefix", {}).get("value", "true") == "true",
        "excludeReference": all_settings.get("search_exclude_reference", {}).get("value", "true") == "true",
        "queryExpansionProfile": all_settings.get("search_query_expansion_profile", {}).get("value", "gu-v1"),
        "rerankMode": all_settings.get("search_rerank_mode", {}).get("value", "none"),
        "hybridRrfK": int(all_settings.get("search_hybrid_rrfk", {}).get("value", "60")),
    }


def update_setting(key: str, value: str, log_change: bool = True) -> dict:
    """
    Update a setting value.

    Args:
        key: Setting key
        value: New value (as string)
        log_change: Whether to log to audit (default True)

    Returns:
        Updated setting dict
    """
    now = datetime.utcnow().isoformat()
    old_value = None

    with _db_lock:
        with get_connection() as conn:
            # Get old value for audit
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,)
            ).fetchone()
            old_value = row["value"] if row else None

            conn.execute("""
                UPDATE settings SET value = ?, updated_at = ?
                WHERE key = ?
            """, (value, now, key))
            conn.commit()

    # Log the change to audit
    if log_change and old_value != value:
        log_audit(
            workflow_id="__system__",
            document_id="__settings__",
            action_type="settings_change",
            entity_type="setting",
            field_name=key,
            old_value=old_value,
            new_value=value
        )

    return get_setting(key)


def update_search_settings(settings: dict) -> dict:
    """
    Update multiple search settings at once.

    Args:
        settings: Dict with keys like searchMethod, limit, alpha, etc.

    Returns:
        Updated search settings
    """
    # Map UI keys to DB keys
    key_map = {
        "searchMethod": "search_method",
        "limit": "search_limit",
        "alpha": "search_alpha",
        "rankingMethod": "search_ranking_method",
        "showHighlights": "search_show_highlights",
        "efSearch": "search_ef_search",
        "indexName": "search_index_name",
        "candidateCap": "search_candidate_cap",
        "candidateMultiplier": "search_candidate_multiplier",
        "maxChunksPerDoc": "search_max_chunks_per_doc",
        "useE5Prefix": "search_use_e5_prefix",
        "excludeReference": "search_exclude_reference",
        "queryExpansionProfile": "search_query_expansion_profile",
        "rerankMode": "search_rerank_mode",
        "hybridRrfK": "search_hybrid_rrfk",
    }

    for ui_key, db_key in key_map.items():
        if ui_key in settings:
            value = settings[ui_key]
            # Convert to string for storage
            if isinstance(value, bool):
                value = "true" if value else "false"
            else:
                value = str(value)
            update_setting(db_key, value)

    return get_search_settings()


def get_settings_audit_logs(limit: int = 50, offset: int = 0) -> list[dict]:
    """Get audit logs for settings changes."""
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM audit_logs
            WHERE document_id = '__settings__'
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
        return [dict(row) for row in rows]


def get_settings_audit_count() -> int:
    """Get count of settings audit logs."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM audit_logs WHERE document_id = '__settings__'"
        ).fetchone()
        return row["cnt"] if row else 0
