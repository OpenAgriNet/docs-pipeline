"""Durable prepared-import state and per-document locks on the shared DB volume.

The API and workers must share the SQLite volume, as they already do for chunks.
POSIX flock survives process boundaries and is released after a process crash;
no database writer lock is held during backend/network operations.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import sqlite3
from pathlib import Path
import uuid

from . import db


def stamp():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def document_lock(workflow_id: str):
    directory = Path(db.DB_PATH).parent / f".{Path(db.DB_PATH).name}-import-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Never use an externally supplied ID as a path component.
    path = directory / hashlib.sha256(workflow_id.encode()).hexdigest()
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another import operation is still running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def publication(workflow_id: str):
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM prepared_imports WHERE workflow_id = ?", (workflow_id,)).fetchone()
        return dict(row) if row else None


def active_upload(instance: str, document_id: str, fingerprint: str):
    with db.get_connection() as conn:
        row = conn.execute("""SELECT * FROM prepared_uploads
            WHERE instance = ? AND (document_id = ? OR fingerprint = ?)
            AND status IN ('uploading', 'cleanup_failed') LIMIT 1""",
            (instance, document_id, fingerprint)).fetchone()
        return dict(row) if row else None


def begin_upload(workflow_id, instance, actor, document_id, fingerprint, objects):
    try:
        with db.get_connection() as conn, conn:
            conn.execute("""INSERT INTO prepared_uploads
                (workflow_id, instance, actor, document_id, fingerprint, objects_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (workflow_id, instance, actor, document_id, fingerprint, json.dumps(objects), stamp()))
    except sqlite3.IntegrityError:
        if active_upload(instance, document_id, fingerprint):
            raise ValueError("Another upload has reserved this document or file") from None
        raise


def finish_upload(workflow_id, *, error=None):
    with db.get_connection() as conn, conn:
        conn.execute("""UPDATE prepared_uploads SET status = ?, error_message = ?
            WHERE workflow_id = ? AND status != 'committed'""",
            ("cleanup_failed" if error else "aborted", error, workflow_id))


def manifest(workflow_id, conn=None):
    """Fingerprint every source field capable of changing the indexed payload."""
    if conn is None:
        with db.get_connection() as connection, connection:
            connection.execute("BEGIN")
            return manifest(workflow_id, connection)
    doc = conn.execute("SELECT * FROM documents WHERE workflow_id = ?", (workflow_id,)).fetchone()
    if not doc:
        raise ValueError("Imported document no longer exists")
    chunks = conn.execute("SELECT * FROM chunks WHERE workflow_id = ? ORDER BY chunk_number", (workflow_id,)).fetchall()
    tags = conn.execute("""SELECT chunk_number, dimension, value FROM chunk_tags
        WHERE workflow_id = ? ORDER BY chunk_number, dimension, value""", (workflow_id,)).fetchall()
    fields = ("chunk_number", "original_text", "edited_text", "token_count", "page_start", "page_end",
              "section_title", "is_reference", "is_excluded", "chunking_provider")
    value = {"document": [doc["document_id"], doc["filename"], doc["instance"]],
             "chunks": [{key: c[key] for key in fields} for c in chunks], "tags": [list(tag) for tag in tags]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def reserve_attempt(workflow_id):
    """Called under document_lock, after any previous Temporal run is terminal."""
    attempt = uuid.uuid4().hex
    runtime_id = f"{workflow_id}-import-{attempt}"
    with db.get_connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        digest = manifest(workflow_id, conn)
        row = conn.execute("SELECT * FROM prepared_imports WHERE workflow_id = ?", (workflow_id,)).fetchone()
        if not row:
            raise ValueError("Import has no pinned DEV destination")
        doc = conn.execute("SELECT * FROM documents WHERE workflow_id = ?", (workflow_id,)).fetchone()
        if doc["is_disabled"]:
            raise ValueError("Restore the document before retrying its import")
        now = stamp()
        cursor = conn.execute("""INSERT INTO document_jobs
            (workflow_id, job_type, temporal_workflow_id, status, current_stage, started_at, config_json)
            VALUES (?, 'prepared_import', ?, 'running', 'ingesting', ?, ?)""",
            (workflow_id, runtime_id, now, json.dumps({"import_attempt_id": attempt, "index_name": row["physical_index"]})))
        job_id = cursor.lastrowid
        conn.execute("""UPDATE prepared_imports SET attempt_id = ?, job_id = ?, manifest_hash = ?,
            status = 'queued', error_message = NULL, updated_at = ? WHERE workflow_id = ?""",
            (attempt, job_id, digest, now, workflow_id))
        conn.execute("""UPDATE documents SET latest_job_id = ?, stage = 'ingesting', query_enabled = 0,
            error_message = NULL, updated_at = ? WHERE workflow_id = ?""", (job_id, now, workflow_id))
    return publication(workflow_id)


def mark(workflow_id, attempt, status, error=None, *, cleanup_required=None):
    with db.get_connection() as conn, conn:
        changed = conn.execute("""UPDATE prepared_imports SET status = ?, error_message = ?, updated_at = ?,
            cleanup_required = COALESCE(?, cleanup_required)
            WHERE workflow_id = ? AND attempt_id = ? AND status != 'verified'""",
            (status, error, stamp(), cleanup_required, workflow_id, attempt)).rowcount
        if not changed:
            return
        pub = conn.execute("SELECT * FROM prepared_imports WHERE workflow_id = ?", (workflow_id,)).fetchone()
        failed = status in {"failed", "cleanup_failed", "start_unknown"}
        stage = "failed" if failed else "ingesting"
        conn.execute("""UPDATE documents SET stage = ?, query_enabled = 0,
            error_message = ?, updated_at = ? WHERE workflow_id = ?""", (stage, error, stamp(), workflow_id))
        conn.execute("""UPDATE document_jobs SET status = ?, current_stage = ?, error_message = ?,
            completed_at = ? WHERE id = ?""",
            ("failed" if failed else "running", stage, error, stamp() if failed else None, pub["job_id"]))


def complete(workflow_id, attempt, count):
    """Caller holds document_lock; recheck the source before publishing success."""
    with db.get_connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        digest = manifest(workflow_id, conn)
        pub = conn.execute("SELECT * FROM prepared_imports WHERE workflow_id = ?", (workflow_id,)).fetchone()
        doc = conn.execute("SELECT * FROM documents WHERE workflow_id = ?", (workflow_id,)).fetchone()
        if not pub or pub["attempt_id"] != attempt or pub["manifest_hash"] != digest or doc["is_disabled"]:
            raise ValueError("Import source or attempt changed during publication")
        now = stamp()
        conn.execute("""UPDATE prepared_imports SET status = 'verified', error_message = NULL, cleanup_required = 0,
            updated_at = ? WHERE workflow_id = ?""", (now, workflow_id))
        conn.execute("""UPDATE documents SET stage = 'completed', query_enabled = 1, error_message = NULL,
            ingested_at = ?, updated_at = ?, reindex_required = 0, reindex_reason = NULL
            WHERE workflow_id = ?""", (now, now, workflow_id))
        conn.execute("""UPDATE document_jobs SET status = 'completed', current_stage = 'completed',
            completed_at = ?, error_message = NULL WHERE id = ?""", (now, pub["job_id"]))
        conn.execute("""INSERT INTO document_index_status
            (workflow_id, index_name, marqo_doc_id, chunk_count_indexed, last_indexed_at, last_verified_at, schema_version, status, details_json)
            VALUES (?, ?, ?, ?, ?, ?, 'passage-v1', 'indexed', ?)
            ON CONFLICT(workflow_id,index_name) DO UPDATE SET chunk_count_indexed = excluded.chunk_count_indexed,
              last_indexed_at = excluded.last_indexed_at, last_verified_at = excluded.last_verified_at,
              status = excluded.status, details_json = excluded.details_json""",
            (workflow_id, pub["physical_index"], doc["document_id"], count, now, now,
             json.dumps({"verified": True, "attempt_id": attempt, "record_count": count})))
