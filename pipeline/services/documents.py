"""Document views, audit records, provenance links, and lifecycle state helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import HTTPException, Request

from .. import db
from ..auth.models import AuthUser
from ..auth.tenancy import assert_document_instance_access, normalize_instance
from ..models import DocumentDetail, DocumentStage, DocumentSummary, PIPELINE_STAGES
from . import workflow_runtime


def normalize_ingest_name(name: str) -> str:
    """Collapse case, spaces, underscores, and punctuation for similar-name matches."""
    return re.sub(r"[^a-z0-9]+", "", Path(name or "").stem.lower())


def similar_ingest_conflict(
    *,
    instance: str,
    filename: str,
    fingerprint: str,
    exclude_workflow_id: Optional[str] = None,
) -> Optional[dict]:
    """Return an existing tenant document that is the same bytes or a similar name."""
    inst = normalize_instance(instance)
    needle = normalize_ingest_name(filename)
    docs = db.list_documents(
        instances=[inst],
        include_disabled=False,
        include_demo=True,
        limit=5000,
    )
    fingerprint = (fingerprint or "").strip()
    for doc in docs:
        if exclude_workflow_id and doc.get("workflow_id") == exclude_workflow_id:
            continue
        same_bytes = bool(fingerprint) and fingerprint in {
            (doc.get("source_file_fingerprint") or "").strip(),
            (doc.get("document_id") or "").strip(),
            (doc.get("canonical_document_id") or "").strip(),
        }
        names = (doc.get("filename"), doc.get("source_filename"), doc.get("display_name"))
        same_name = bool(needle) and any(
            normalize_ingest_name(n) == needle for n in names if n
        )
        if same_bytes or same_name:
            return doc
    return None


def similar_ingest_http_detail(existing: dict) -> dict:
    name = existing.get("filename") or existing.get("source_filename") or existing["workflow_id"]
    return {
        "code": "similar_document_exists",
        "message": (
            f"A similar document already exists ({name}). "
            "Confirm to ingest this file as a new run."
        ),
        "existing_workflow_id": existing["workflow_id"],
        "existing_filename": name,
        "existing_stage": existing.get("stage"),
    }


async def dedup_or_none(
    user: AuthUser,
    workflow_id: str,
    *,
    document_id: str,
    canonical_document_id: str,
    filename: str,
    source_filename: str,
    source_file_fingerprint: str,
    force: bool = False,
) -> tuple[Optional[DocumentSummary], str]:
    """Shared ingest dedup for ``POST /documents`` and ``POST /upload``.

    Reuse only when SQLite still tracks ``workflow_id`` **and** Temporal still
    answers ``get_state``. Otherwise the caller starts a new run.

    Returns ``(summary, workflow_id_to_use)``:
    - Dedup hit → ``(DocumentSummary(duplicate=True, …), workflow_id)``.
    - Miss → ``(None, workflow_id_to_use)``. Prefers the stable ``workflow_id``
      so a later identical ingest can hit. Allocates ``*-rerun-*`` only when
      ``force`` is set, or when SQLite has no row but Temporal still answers
      for that id (orphan execution after a SQLite purge).

    ``force`` skips reuse (always allocate a rerun id). Not wired to HTTP yet;
    kept so both ingest doors stay on one helper when a force flag is added.
    """
    if force:
        return None, workflow_runtime.rerun_workflow_id(workflow_id)

    existing_doc = db.get_document(workflow_id)
    if existing_doc:
        # Same fingerprint/path must not leak or restart another tenant's doc.
        existing_doc = assert_document_instance_access(user, existing_doc)
        try:
            state = await workflow_runtime.query_workflow_state(workflow_id)
            if state:
                return (
                    DocumentSummary(
                        document_id=document_id,
                        canonical_document_id=canonical_document_id,
                        workflow_id=workflow_id,
                        filename=filename,
                        source_filename=source_filename,
                        source_file_fingerprint=source_file_fingerprint,
                        authoritative=bool(existing_doc.get("source_manifest_name")),
                        instance=normalize_instance(existing_doc.get("instance")),
                        stage=DocumentStage(state.get("stage", "registered")),
                        page_count=state.get("page_count", 0),
                        chunk_count=state.get("chunk_count", 0),
                        error_message=state.get("error_message"),
                        duplicate=True,
                    ),
                    workflow_id,
                )
        except HTTPException:
            raise
        except Exception:
            pass  # Workflow not queryable; reclaim the same id below
        return None, workflow_id

    # No SQLite row. Keep the stable id unless Temporal still holds it (purge
    # orphan) — otherwise every first ingest stored ``*-rerun-*`` and dedup
    # against the path-derived id could never hit.
    try:
        state = await workflow_runtime.query_workflow_state(workflow_id)
        if state:
            return None, workflow_runtime.rerun_workflow_id(workflow_id)
    except HTTPException:
        raise
    except Exception:
        pass
    return None, workflow_id


def document_summary_from_row(
    doc: dict, current_job: Optional[dict] = None
) -> DocumentSummary:
    return DocumentSummary(
        document_id=doc["document_id"],
        canonical_document_id=doc.get("canonical_document_id"),
        workflow_id=doc["workflow_id"],
        filename=doc["filename"],
        display_name=doc.get("display_name"),
        source_filename=doc.get("source_filename"),
        source_manifest_name=doc.get("source_manifest_name"),
        source_file_fingerprint=doc.get("source_file_fingerprint"),
        authoritative=bool(doc.get("source_manifest_name")),
        instance=normalize_instance(doc.get("instance")),
        is_demo=bool(doc.get("is_demo")),
        is_disabled=bool(doc.get("is_disabled")),
        query_enabled=(
            bool(doc["query_enabled"]) if doc.get("query_enabled") is not None else True
        ),
        stage=DocumentStage(doc["stage"]),
        page_count=doc.get("page_count") or 0,
        chunk_count=doc.get("chunk_count") or 0,
        error_message=doc.get("error_message"),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
        reindex_required=bool(doc.get("reindex_required")),
        reindex_reason=doc.get("reindex_reason"),
        available_actions=list_available_actions(
            doc,
            current_job
            if current_job is not None
            else db.get_latest_document_job(doc["workflow_id"]),
        ),
    )


def provenance_base_urls(request: Request) -> tuple[str, str]:
    api_base = (os.environ.get("DOCS_PIPELINE_API_URL") or str(request.base_url)).rstrip("/")
    ui_base = (os.environ.get("DOCS_PIPELINE_UI_URL") or "http://localhost:3000").rstrip("/")
    return api_base, ui_base


def build_provenance_links(
    workflow_id: str, chunk_num: int, request: Request
) -> dict[str, str]:
    api_base, ui_base = provenance_base_urls(request)
    return {
        "pdf_url": f"{api_base}/documents/{workflow_id}/pdf",
        "document_url": f"{ui_base}/documents/{workflow_id}",
        "chunk_url": f"{ui_base}/documents/{workflow_id}?tab=chunks&chunk={chunk_num}",
    }


def list_available_actions(doc: dict, current_job: Optional[dict] = None) -> list[str]:
    if not doc:
        return []
    if doc.get("is_disabled"):
        return ["restore_document"]

    stage = doc.get("stage")
    actions = ["disable_document", "reconcile_document", "set_query_enabled", "set_metadata"]
    if stage == "ocr_review":
        actions.append("approve_ocr")
        actions.append("force_ocr")
    elif stage == "translation_review":
        actions.append("approve_translation")
    elif stage == "chunk_review":
        actions.append("approve_chunks")
    elif stage == "ready_for_ingestion":
        actions.append("approve_ingestion")
    elif stage == "completed":
        if reingest_allowed(doc):
            actions.append("reingest_document")
    elif stage == "failed":
        if not doc.get("ocr_completed_at"):
            actions.append("retry_ocr")
        # Force redo when pages already exist (bad OCR) or OCR had completed.
        if doc.get("ocr_completed_at") or (doc.get("page_count") or 0) > 0:
            actions.append("force_ocr")
        if doc.get("ocr_completed_at") and not doc.get("translation_completed_at"):
            actions.append("retry_translation")
        if doc.get("translation_completed_at"):
            actions.append("retry_chunking")

    if doc.get("reindex_required"):
        if reingest_allowed(doc):
            actions.append("reingest_document")
        actions.append("clear_reindex_required")
    else:
        actions.append("mark_reindex_required")
    if current_job and current_job.get("status") == "running":
        actions.append("inspect_runtime")
    return sorted(set(actions))


def reingest_allowed(doc: dict) -> bool:
    """Reingest is blocked while force re-OCR has invalidated downstream state.

    Force clears chunk state before the workflow starts, so the stage can still
    read ``completed`` for the moment between the API call and the worker
    advancing it. Requiring a rebuilt chunk set closes that window: republishing
    is safe again only once chunking has finished for the new OCR text.
    """
    doc = doc or {}
    if doc.get("reindex_reason") != "force_ocr_requested":
        return True
    if not doc.get("chunks_completed_at"):
        return False
    return doc.get("stage") in {"chunk_review", "ready_for_ingestion", "completed"}


def mark_reindex_required(
    workflow_id: str, reason: str, metadata: Optional[dict] = None
) -> Optional[dict]:
    doc = db.mark_document_reindex_required(workflow_id, True, reason)
    if doc:
        db.log_audit(
            workflow_id=workflow_id,
            document_id=doc.get("document_id", workflow_id),
            action_type="mark_reindex_required",
            field_name="reindex_required",
            old_value="false",
            new_value="true",
            metadata={"reason": reason, **(metadata or {})},
        )
    return doc


def build_document_detail(doc: dict) -> DocumentDetail:
    workflow_id = doc["workflow_id"]
    current_job = db.get_latest_document_job(workflow_id)
    return DocumentDetail(
        document_id=doc["document_id"],
        canonical_document_id=doc.get("canonical_document_id"),
        workflow_id=workflow_id,
        filename=doc["filename"],
        display_name=doc.get("display_name"),
        source_filename=doc.get("source_filename"),
        source_manifest_name=doc.get("source_manifest_name"),
        source_file_fingerprint=doc.get("source_file_fingerprint"),
        authoritative=bool(doc.get("source_manifest_name")),
        instance=normalize_instance(doc.get("instance")),
        filepath=doc["filepath"],
        stage=DocumentStage(doc["stage"]),
        page_count=doc.get("page_count", 0),
        chunk_count=doc.get("chunk_count", 0),
        error_message=doc.get("error_message"),
        reindex_required=bool(doc.get("reindex_required")),
        reindex_reason=doc.get("reindex_reason"),
        available_actions=list_available_actions(doc, current_job),
        translated_count=sum(
            1 for page in db.get_pages(workflow_id) if page.get("translated_markdown")
        ),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
        ocr_completed_at=doc.get("ocr_completed_at"),
        translation_completed_at=doc.get("translation_completed_at"),
        chunks_completed_at=doc.get("chunks_completed_at"),
        ingested_at=doc.get("ingested_at"),
        source_type=doc.get("source_type"),
        canonical_input_type=doc.get("canonical_input_type"),
        stop_after_ocr=bool(doc.get("stop_after_ocr")),
        original_artifact_id=doc.get("original_artifact_id"),
        normalized_artifact_id=doc.get("normalized_artifact_id"),
        latest_job_id=doc.get("latest_job_id"),
        current_job=current_job,
        artifacts=db.list_document_artifacts(workflow_id),
        index_status=db.list_document_index_status(workflow_id),
    )


def build_stage_io_payload(workflow_id: str, current_stage: Optional[str] = None) -> dict:
    artifacts = db.list_document_artifacts(workflow_id)
    grouped: dict[str, dict] = {}
    for stage_id, label, description in PIPELINE_STAGES:
        grouped[stage_id] = {
            "stage": stage_id,
            "label": label,
            "description": description,
            "input_artifacts": [],
            "output_artifacts": [],
        }

    input_types = {"original_upload", "normalized_pdf", "normalized_spreadsheet", "normalized_docx"}
    for artifact in artifacts:
        stage = artifact.get("stage") or "registered"
        if stage not in grouped:
            grouped[stage] = {
                "stage": stage,
                "label": stage.replace("_", " ").title(),
                "description": "",
                "input_artifacts": [],
                "output_artifacts": [],
            }
        bucket = (
            "input_artifacts"
            if artifact["artifact_type"] in input_types
            else "output_artifacts"
        )
        grouped[stage][bucket].append(artifact)

    return {
        "workflow_id": workflow_id,
        "current_stage": current_stage,
        "stages": list(grouped.values()),
    }


def log_audit(
    workflow_id: str,
    action_type: str,
    entity_type: str = None,
    entity_id: int = None,
    field_name: str = None,
    old_value=None,
    new_value=None,
    metadata: dict = None,
):
    """Write an audit entry, JSON-serializing structured old/new values."""
    doc = db.get_document(workflow_id)
    document_id = doc["document_id"] if doc else workflow_id
    old_str = (
        json.dumps(old_value)
        if old_value is not None and not isinstance(old_value, str)
        else old_value
    )
    new_str = (
        json.dumps(new_value)
        if new_value is not None and not isinstance(new_value, str)
        else new_value
    )
    db.log_audit(
        workflow_id=workflow_id,
        document_id=document_id,
        action_type=action_type,
        entity_type=entity_type,
        entity_id=entity_id,
        field_name=field_name,
        old_value=old_str,
        new_value=new_str,
        metadata=metadata,
    )


def inline_content_disposition(filename: str) -> str:
    """Build a latin-1-safe Content-Disposition header for inline display."""
    safe_name = (filename or "document.pdf").replace('"', "'")
    safe_name = "".join(ch for ch in safe_name if ch not in "\r\n\0").strip() or "document.pdf"
    try:
        safe_name.encode("latin-1")
        return f'inline; filename="{safe_name}"'
    except UnicodeEncodeError:
        ascii_name = safe_name.encode("ascii", "ignore").decode("ascii").strip() or "document.pdf"
        encoded_name = quote(safe_name)
        return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"
