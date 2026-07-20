"""
FastAPI REST API for the Temporal-based OCR pipeline.

This API provides HTTP endpoints that interact with Temporal workflows.
"""

import os
import json
import asyncio
import hashlib
import logging
import math
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager
from io import BytesIO

from fastapi import FastAPI, HTTPException, Query, Path as PathParam, UploadFile, File, Header, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from marqo.errors import MarqoError
from minio import Minio
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from urllib.parse import quote

from .models import (
    RegisterRequest, RegisterFolderRequest, PageUpdate, ChunkUpdate, ChunkTagsUpdate,
    ApprovalRequest, DocumentDetail, DocumentSummary, DocumentStage, PIPELINE_STAGES,
    AuditLogResponse, SearchSettings, SearchSettingsUpdate, SettingsAuditResponse,
    DocumentCohortsResponse, OperationQueueEntry, OperationQueueResponse,
    BulkWorkflowActionRequest, BulkWorkflowActionResponse, BulkWorkflowActionResult,
    DocumentGraph, ReindexStateRequest,
)
from .workflows import (
    DocumentPipelineWorkflow,
    ReingestionWorkflow,
    TranslationOnlyWorkflow,
    OcrOnlyWorkflow,
    ChunkingOnlyWorkflow,
)
from . import db
from .auth.deps import CurrentUser, RequireAdmin, RequirePipeline, RequireReview, RequireUpload
from .auth.models import AuthUser
from .auth.config import load_auth_config, validate_auth_config
from .auth.tenancy import (
    allowed_instances,
    assert_document_instance_access,
    assert_instance_access,
    default_instance,
    normalize_instance,
    user_can_access_instance,
)

TASK_QUEUE = "ocr-pipeline"
_TOKEN_RE = re.compile(r"[\w\-]+", re.UNICODE)

# Global clients
temporal_client: Optional[Client] = None
minio_client: Optional[Minio] = None
MINIO_BUCKET = "documents"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Temporal and MinIO clients on startup."""
    global temporal_client, minio_client, MINIO_BUCKET

    auth_cfg = load_auth_config()
    validate_auth_config(auth_cfg)
    if auth_cfg.disabled:
        logging.warning(
            "WARNING: AUTH_DISABLED=true — every caller is treated as synthetic "
            "master_admin with unrestricted instance access. Do not expose this "
            "API beyond the internal network or set AUTH_DISABLED=false until "
            "the maintainer UI sends Bearer tokens."
        )
    else:
        logging.info(
            "Auth enabled: issuer=%s audience=%s jwks=%s",
            auth_cfg.keycloak_issuer,
            auth_cfg.keycloak_audience or "(none)",
            auth_cfg.keycloak_jwks_url,
        )

    # Initialize SQLite database
    print("Initializing SQLite database...")
    db.init_db()

    # Temporal
    temporal_host = os.environ.get("TEMPORAL_HOST", "localhost:7233")
    print(f"Connecting to Temporal at {temporal_host}")
    temporal_client = await Client.connect(temporal_host)

    # MinIO - credentials required via environment variables
    minio_endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    minio_access_key = os.environ.get("MINIO_ACCESS_KEY")
    minio_secret_key = os.environ.get("MINIO_SECRET_KEY")
    MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "documents")

    if not minio_access_key or not minio_secret_key:
        raise RuntimeError("MINIO_ACCESS_KEY and MINIO_SECRET_KEY environment variables are required")

    print(f"Connecting to MinIO at {minio_endpoint}")
    minio_client = Minio(
        minio_endpoint,
        access_key=minio_access_key,
        secret_key=minio_secret_key,
        secure=False
    )

    # Ensure bucket exists
    if not minio_client.bucket_exists(MINIO_BUCKET):
        minio_client.make_bucket(MINIO_BUCKET)
        print(f"Created MinIO bucket: {MINIO_BUCKET}")

    yield
    # Cleanup if needed


app = FastAPI(
    title="Document Ingestion Pipeline API",
    description="""
REST API for the Temporal-based document OCR pipeline with translation support.

## Workflow Stages

1. `registered` - Document registered
2. `ocr_processing` - OCR in progress
3. `ocr_review` - **Waiting for OCR review/approval**
4. `translation_processing` - Translating non-English content
5. `translation_review` - **Waiting for translation review/approval**
6. `chunking` - Chunking in progress
7. `chunk_review` - **Waiting for chunk review/approval**
8. `ready_for_ingestion` - **Waiting for final approval**
9. `ingesting` - Ingesting to Marqo
10. `completed` - Done
11. `failed` - Error occurred

## Review Flow

1. Start workflow with `POST /upload` or `POST /documents`
2. Wait for `ocr_review` stage
3. Review/edit pages with `GET/PATCH /documents/{id}/pages/{num}`
4. Approve with `POST /documents/{id}/approve-ocr`
5. Wait for `translation_review` stage
6. Review/edit translations with `PATCH /documents/{id}/pages/{num}`
7. Approve with `POST /documents/{id}/approve-translation`
8. Wait for `chunk_review` stage
9. Review/edit chunks with `GET/PATCH /documents/{id}/chunks/{num}`
10. Approve with `POST /documents/{id}/approve-chunks`
11. Wait for `ready_for_ingestion` stage
12. Final approval with `POST /documents/{id}/approve-ingestion`
13. Workflow completes automatically
    """,
    version="2.0.0",
    lifespan=lifespan
)

# CORS configuration - explicit origins for security
ALLOWED_ORIGINS = os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Rate limiting configuration
# Default: 100 requests/minute for general endpoints, 10/minute for uploads
RATE_LIMIT_DEFAULT = os.environ.get("RATE_LIMIT_DEFAULT", "100/minute")
RATE_LIMIT_UPLOAD = os.environ.get("RATE_LIMIT_UPLOAD", "10/minute")

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Allowed base directories for file access (configurable via env)
ALLOWED_FILE_PATHS = os.environ.get("ALLOWED_FILE_PATHS", "/app/books,/data/documents").split(",")
ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"
}


def validate_file_path(filepath: str) -> str:
    """
    Validate that a file path is within allowed directories.
    Prevents path traversal attacks.

    Returns the validated filepath as a string.
    Raises HTTPException if path is not allowed.
    """
    # Handle minio:// URIs - these are validated by MinIO access
    if filepath.startswith("minio://"):
        suffix = Path(filepath).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(400, f"Unsupported file type: {suffix}")
        return filepath  # Return string as-is, MinIO handles validation

    path = Path(filepath).resolve()  # Resolve to absolute, canonical path

    # Check if path is within any allowed directory
    for allowed_base in ALLOWED_FILE_PATHS:
        allowed_path = Path(allowed_base.strip()).resolve()
        try:
            path.relative_to(allowed_path)
            # Path is within allowed directory
            if not path.exists():
                raise HTTPException(404, "File not found")
            if not path.is_file():
                raise HTTPException(400, "Path is not a file")
            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                raise HTTPException(400, f"Unsupported file type: {path.suffix.lower()}")
            return str(path)
        except ValueError:
            continue  # Not within this allowed path, try next

    # Path not within any allowed directory
    raise HTTPException(403, "Access to this file path is not allowed")


def get_filename_from_path(filepath: str) -> str:
    """Extract filename from a filepath string (works for both local and minio:// paths)."""
    if filepath.startswith("minio://"):
        # minio://bucket/path/to/file.pdf -> file.pdf
        return filepath.split("/")[-1]
    return Path(filepath).name


def get_workflow_id(filepath: str) -> str:
    """Generate consistent workflow ID from filepath."""
    return f"doc-{hashlib.md5(filepath.encode()).hexdigest()[:12]}"


def _rerun_workflow_id(base_workflow_id: str) -> str:
    """Generate a fresh workflow ID for explicit reruns of the same source."""
    return f"{base_workflow_id}-rerun-{int(time.time())}"


def _compute_file_fingerprint(filepath: Path) -> str:
    md5 = hashlib.md5()
    with filepath.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(chunk)
    return md5.hexdigest()


def get_marqo_doc_id(document_id: str) -> str:
    """Document identifier stored in the Marqo doc_id field."""
    return document_id


def get_legacy_marqo_doc_id(document_id: str) -> str:
    """Legacy hashed doc_id used before provenance ingest alignment."""
    return hashlib.md5(document_id.encode()).hexdigest()


def _ignore_client_marqo_url(_client_supplied: str = "") -> str:
    """Always resolve Marqo from MARQO_URL at ingest time; ignore client URLs (SSRF)."""
    return ""


def _instance_scope_for_user(user: AuthUser) -> Optional[list[str]]:
    """None = unrestricted; otherwise only these instance ids."""
    allowed = allowed_instances(user)
    if allowed is None:
        return None
    return sorted(allowed)


def _resolve_create_instance(user: AuthUser, requested: Optional[str] = None) -> str:
    """Normalize/create-time instance and ensure the caller may use it."""
    return assert_instance_access(user, requested or default_instance())


def _require_document_for_user(workflow_id: str, user: AuthUser) -> dict:
    """Load a document or 404 if missing / outside the caller's instance scope."""
    return assert_document_instance_access(user, db.get_document(workflow_id))


def _document_for_user_or_none(workflow_id: str, user: AuthUser) -> Optional[dict]:
    """Like _require_document_for_user but returns None instead of raising (bulk paths)."""
    doc = db.get_document(workflow_id)
    if not doc or not user_can_access_instance(user, doc.get("instance")):
        return None
    return doc


def _document_summary_from_row(doc: dict, current_job: Optional[dict] = None) -> DocumentSummary:
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
        stage=DocumentStage(doc["stage"]),
        page_count=doc.get("page_count") or 0,
        chunk_count=doc.get("chunk_count") or 0,
        error_message=doc.get("error_message"),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
        reindex_required=bool(doc.get("reindex_required")),
        reindex_reason=doc.get("reindex_reason"),
        available_actions=_list_available_actions(
            doc,
            current_job if current_job is not None else db.get_latest_document_job(doc["workflow_id"]),
        ),
    )


def _provenance_base_urls(request: Request) -> tuple[str, str]:
    api_base = (os.environ.get("DOCS_PIPELINE_API_URL") or str(request.base_url)).rstrip("/")
    ui_base = (os.environ.get("DOCS_PIPELINE_UI_URL") or "http://localhost:3000").rstrip("/")
    return api_base, ui_base


def _build_provenance_links(workflow_id: str, chunk_num: int, request: Request) -> dict[str, str]:
    api_base, ui_base = _provenance_base_urls(request)
    return {
        "pdf_url": f"{api_base}/documents/{workflow_id}/pdf",
        "document_url": f"{ui_base}/documents/{workflow_id}",
        "chunk_url": f"{ui_base}/documents/{workflow_id}?tab=chunks&chunk={chunk_num}",
    }


def _list_available_actions(doc: dict, current_job: Optional[dict] = None) -> list[str]:
    if not doc:
        return []
    if doc.get("is_disabled"):
        return ["restore_document"]

    stage = doc.get("stage")
    actions = ["disable_document", "reconcile_document"]
    if stage == "ocr_review":
        actions.append("approve_ocr")
    elif stage == "translation_review":
        actions.append("approve_translation")
    elif stage == "chunk_review":
        actions.append("approve_chunks")
    elif stage == "ready_for_ingestion":
        actions.append("approve_ingestion")
    elif stage == "completed":
        actions.append("reingest_document")
    elif stage == "failed":
        if not doc.get("ocr_completed_at"):
            actions.append("retry_ocr")
        if doc.get("ocr_completed_at") and not doc.get("translation_completed_at"):
            actions.append("retry_translation")
        if doc.get("translation_completed_at"):
            actions.append("retry_chunking")

    if doc.get("reindex_required"):
        actions.extend(["reingest_document", "clear_reindex_required"])
    else:
        actions.append("mark_reindex_required")

    if current_job and current_job.get("status") == "running":
        actions.append("inspect_runtime")

    return sorted(set(actions))


def _mark_reindex_required(workflow_id: str, reason: str, metadata: Optional[dict] = None) -> Optional[dict]:
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


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _tokenize(value: str) -> list[str]:
    return _TOKEN_RE.findall(_normalize_text(value))


def _prepare_query_for_e5(query: str) -> str:
    cleaned = query.strip()
    if cleaned.lower().startswith("query:"):
        return cleaned
    return f"query: {cleaned}"


def _token_overlap_score(query: str, text: str) -> float:
    query_tokens = set(_tokenize(query))
    text_tokens = set(_tokenize(text))
    if not query_tokens or not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def _metadata_blob(hit: dict) -> str:
    return " ".join(
        str(hit.get(key) or "")
        for key in (
            "name",
            "name_en",
            "name_gu",
            "filename",
            "title_en",
            "title_gu",
            "category_tags",
            "description",
            "doc_short_description",
            "doc_llm_description",
        )
    )


def _rank_desc(values: list[float]) -> list[int]:
    order = sorted(range(len(values)), key=lambda idx: values[idx], reverse=True)
    ranks = [0] * len(values)
    for pos, idx in enumerate(order, start=1):
        ranks[idx] = pos
    return ranks


def _expand_query(query: str, profile: str) -> str:
    q = (query or "").strip()
    mode = (profile or "none").strip().lower()
    if not q or mode in {"none", ""}:
        return q
    if mode not in {"gu-v1", "gu_v1"}:
        return q

    rules = [
        (r"ખરવા|મોવાસા|fmd", "foot and mouth disease FMD blisters lesions mouth ulcer"),
        (r"આફરો|bloat", "ruminal bloat tympany frothy bloat"),
        (r"તાવ|fever", "pyrexia febrile infection"),
        (r"કબજ|constipation", "constipation bowel obstruction laxative"),
        (r"ગળિયો|ગળાની", "throat infection pharyngitis upper respiratory"),
        (r"કૃમિ|કરમિયા|deworm", "deworming helminth anthelmintic dose"),
        (r"ગર્ભપાત|ગાભણ", "abortion pregnancy gestation prenatal feeding"),
        (r"ચરમિયા|ચામડી|ખંજવાળ|hair fall", "dermatitis skin disease mange ectoparasite tick"),
    ]

    additions: list[str] = []
    query_lower = q.lower()
    for pattern, terms in rules:
        if re.search(pattern, query_lower, flags=re.IGNORECASE):
            additions.append(terms)
    if not additions:
        return q
    return f"{q} {' '.join(additions)}".strip()


def _bm25lite_scores(query: str, docs: list[str]) -> list[float]:
    query_tokens = _tokenize(query)
    if not query_tokens or not docs:
        return [0.0] * len(docs)

    doc_tokens = [_tokenize(doc) for doc in docs]
    avg_len = max(1.0, sum(len(tokens) for tokens in doc_tokens) / max(1, len(doc_tokens)))
    df: Counter[str] = Counter()
    for tokens in doc_tokens:
        for token in set(tokens):
            df[token] += 1

    k1 = 1.2
    b = 0.75
    scores: list[float] = []
    for tokens in doc_tokens:
        tf = Counter(tokens)
        dl = len(tokens)
        norm = k1 * (1 - b + b * dl / avg_len)
        score = 0.0
        for term in query_tokens:
            if term not in tf:
                continue
            idf = math.log(1.0 + (len(doc_tokens) - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * (tf[term] * (k1 + 1.0)) / (tf[term] + norm)
        scores.append(score)
    return scores


def _rerank_hits(query: str, hits: list[dict], rerank_mode: str) -> list[dict]:
    mode = (rerank_mode or "none").strip().lower()
    if mode in {"", "none"} or not hits:
        return hits

    raw_scores = [float(hit.get("_score", hit.get("score", 0.0)) or 0.0) for hit in hits]
    min_score = min(raw_scores)
    max_score = max(raw_scores)
    denom = (max_score - min_score) if max_score > min_score else 1.0
    semantic_scores = [(raw - min_score) / denom for raw in raw_scores]
    text_scores = [_token_overlap_score(query, str(hit.get("text") or "")) for hit in hits]
    meta_scores = [_token_overlap_score(query, _metadata_blob(hit)) for hit in hits]

    rescored: list[dict] = []
    if mode == "bm25lite":
        docs = [f"{str(hit.get('text') or '')} {_metadata_blob(hit)}".strip() for hit in hits]
        bm_scores = _bm25lite_scores(query, docs)
        bm_min = min(bm_scores) if bm_scores else 0.0
        bm_max = max(bm_scores) if bm_scores else 1.0
        bm_denom = (bm_max - bm_min) if bm_max > bm_min else 1.0
        bm_norm = [(score - bm_min) / bm_denom for score in bm_scores]
        for hit, semantic, bm25, meta in zip(hits, semantic_scores, bm_norm, meta_scores):
            enriched = dict(hit)
            enriched["_rerank_score"] = (0.50 * semantic) + (0.40 * bm25) + (0.10 * meta) + (-0.10 if bool(hit.get("is_reference", False)) else 0.0)
            rescored.append(enriched)
    elif mode in {"rrf-lite", "rrf_lite", "rrf"}:
        sem_rank = _rank_desc(semantic_scores)
        text_rank = _rank_desc(text_scores)
        meta_rank = _rank_desc(meta_scores)
        k = 30
        for idx, hit in enumerate(hits):
            enriched = dict(hit)
            enriched["_rerank_score"] = (1.0 / (k + sem_rank[idx])) + (1.0 / (k + text_rank[idx])) + (1.0 / (k + meta_rank[idx]))
            rescored.append(enriched)
    else:
        for hit, semantic, text_score, meta in zip(hits, semantic_scores, text_scores, meta_scores):
            enriched = dict(hit)
            enriched["_rerank_score"] = (0.60 * semantic) + (0.30 * max(text_score, meta)) + (0.10 * meta)
            rescored.append(enriched)

    rescored.sort(key=lambda hit: float(hit.get("_rerank_score", 0.0)), reverse=True)
    return rescored


def delete_single_chunk_from_marqo(document_id: str, chunk_num: int, index_name: str = "documents-index") -> dict:
    """
    Delete a single chunk from Marqo by doc_id and chunk_num.

    Args:
        doc_id: The document_id hash used in Marqo's doc_id field
        chunk_num: The chunk number to delete
        index_name: Marqo index name

    Returns:
        Dict with deletion result
    """
    import marqo

    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    mq = marqo.Client(url=marqo_url)

    try:
        index = mq.index(index_name)

        # Search for the specific chunk
        marqo_doc_id = get_marqo_doc_id(document_id)
        results = index.search(
            q="",
            filter_string=f"doc_id:{marqo_doc_id} AND chunk_num:{chunk_num}",
            limit=1,
            attributes_to_retrieve=["_id"]
        )

        if not results.get("hits"):
            return {"deleted": False, "reason": "not_found"}

        # Delete the chunk
        chunk_id = results["hits"][0]["_id"]
        index.delete_documents(ids=[chunk_id])

        return {"deleted": True, "chunk_id": chunk_id}

    except Exception as e:
        return {"deleted": False, "error": str(e)}


def delete_chunks_from_marqo(document_id: str, index_name: str = "documents-index") -> dict:
    """
    Delete all chunks for a document from Marqo.

    Args:
        doc_id: The document_id hash used in Marqo's doc_id field
        index_name: Marqo index name

    Returns:
        Dict with deletion stats
    """
    import marqo

    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    mq = marqo.Client(url=marqo_url)

    try:
        index = mq.index(index_name)

        # Search for all documents with this doc_id
        # Marqo doesn't have delete by filter, so we need to find IDs first
        marqo_doc_id = get_marqo_doc_id(document_id)
        results = index.search(
            q="",
            filter_string=f"doc_id:{marqo_doc_id}",
            limit=1000,  # Get all chunks for this document
            attributes_to_retrieve=["_id"]
        )

        if not results.get("hits"):
            return {"deleted": 0, "doc_id": marqo_doc_id}

        # Extract IDs and delete
        ids_to_delete = [hit["_id"] for hit in results["hits"]]
        if ids_to_delete:
            index.delete_documents(ids=ids_to_delete)

        return {"deleted": len(ids_to_delete), "doc_id": marqo_doc_id}

    except Exception as e:
        # Index might not exist or other error
        return {"deleted": 0, "doc_id": document_id, "error": str(e)}


# =============================================================================
# Document Routes
# =============================================================================

@app.get("/auth/me")
async def auth_me(user: CurrentUser):
    """Return the authenticated caller (local bypass user when AUTH_DISABLED=true)."""
    return {
        "user_id": user.user_id,
        "username": user.username,
        "email": user.email,
        "roles": user.roles,
        "permissions": sorted(p.value for p in user.permissions),
        "instances": user.instances,
        "envs": user.envs,
        "auth_disabled": user.token_disabled_mode,
    }


@app.post("/documents", response_model=DocumentSummary)
@limiter.limit(RATE_LIMIT_UPLOAD)
async def start_document_workflow(
    request: Request,  # Required for rate limiting
    data: RegisterRequest,
    user: RequireUpload,
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    marqo_url: str = "",  # Ignored; MARQO_URL env is used at ingest (SSRF)
    index_name: str = "documents-index",
    stop_after_ocr: bool = False,
    instance: str = "",
):
    """
    Start a new document processing workflow.

    The workflow will:
    1. Run OCR
    2. Wait for approval (unless auto_approve=True)
    3. Create chunks
    4. Wait for approval (unless auto_approve=True)
    5. Ingest to Marqo

    Note: File path must be within allowed directories (ALLOWED_FILE_PATHS env var).
    Rate limited to 10 requests/minute per IP.
    Client-supplied marqo_url is ignored; ingest uses MARQO_URL from the environment.
    Requires permission: upload (no-op while AUTH_DISABLED=true).
    """
    create_instance = _resolve_create_instance(user, instance)
    marqo_url = _ignore_client_marqo_url(marqo_url)
    # Validate file path to prevent path traversal attacks
    filepath = validate_file_path(data.filepath)
    source_filename = get_filename_from_path(filepath)
    source_file_fingerprint = _compute_file_fingerprint(filepath)
    canonical_document_id = source_file_fingerprint

    workflow_id = get_workflow_id(str(filepath))
    document_id = canonical_document_id

    # Reuse only when SQLite still tracks this workflow.
    # If SQLite was purged, avoid returning stale Temporal state and create a fresh run ID.
    existing_doc = db.get_document(workflow_id)
    if existing_doc:
        # Same fingerprint/path must not leak or restart another tenant's doc.
        existing_doc = assert_document_instance_access(user, existing_doc)
        try:
            handle = temporal_client.get_workflow_handle(workflow_id)
            state = await handle.query("get_state")
            if state:
                return DocumentSummary(
                    document_id=document_id,
                    canonical_document_id=canonical_document_id,
                    workflow_id=workflow_id,
                    filename=source_filename,
                    source_filename=source_filename,
                    source_file_fingerprint=source_file_fingerprint,
                    authoritative=bool(existing_doc.get("source_manifest_name")) if existing_doc else False,
                    instance=normalize_instance(existing_doc.get("instance")),
                    stage=DocumentStage(state.get("stage", "registered")),
                    page_count=state.get("page_count", 0),
                    chunk_count=state.get("chunk_count", 0),
                    error_message=state.get("error_message"),
                )
        except HTTPException:
            raise
        except Exception:
            pass  # Workflow doesn't exist or is not queryable; proceed to new run
    else:
        workflow_id = _rerun_workflow_id(workflow_id)

    # Start new workflow
    handle = await temporal_client.start_workflow(
        DocumentPipelineWorkflow.run,
        args=[
            document_id,
            get_filename_from_path(filepath),
            str(filepath),
            chunk_size,
            chunk_overlap,
            min_tokens,
            marqo_url,
            index_name,
            auto_approve,
            stop_after_ocr,
        ],
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )

    # Save to SQLite for visibility during processing
    db.upsert_document(
        workflow_id=workflow_id,
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        filename=source_filename,
        source_filename=source_filename,
        source_file_fingerprint=source_file_fingerprint,
        filepath=str(filepath),
        stage="registered",
        stop_after_ocr=stop_after_ocr,
        instance=create_instance,
    )
    job_id = db.create_document_job(
        workflow_id=workflow_id,
        job_type="ocr_only" if stop_after_ocr else "pipeline",
        temporal_workflow_id=workflow_id,
        status="running",
        current_stage="registered",
        config={
            "auto_approve": auto_approve,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "min_tokens": min_tokens,
            "index_name": index_name,
            "stop_after_ocr": stop_after_ocr,
        },
    )
    db.update_document_fields(workflow_id, latest_job_id=job_id)

    return DocumentSummary(
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        workflow_id=workflow_id,
        filename=source_filename,
        source_filename=source_filename,
        source_file_fingerprint=source_file_fingerprint,
        authoritative=False,
        instance=create_instance,
        stage=DocumentStage.REGISTERED,
        page_count=0,
        chunk_count=0,
    )


@app.post("/upload", response_model=DocumentSummary)
@limiter.limit(RATE_LIMIT_UPLOAD)
async def upload_and_process(
    request: Request,  # Required for rate limiting
    user: RequireUpload,
    file: UploadFile = File(...),
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    marqo_url: str = "",
    index_name: str = "documents-index",
    stop_after_ocr: bool = False,
    instance: str = "",
):
    """
    Upload a supported file and start processing workflow.

    The file is stored in MinIO and then processed through the pipeline.
    Validates both file extension and PDF magic bytes for security.
    Rate limited to 10 requests/minute per IP.
    Client-supplied marqo_url is ignored; ingest uses MARQO_URL from the environment.
    Requires permission: upload (no-op while AUTH_DISABLED=true).
    """
    create_instance = _resolve_create_instance(user, instance)
    marqo_url = _ignore_client_marqo_url(marqo_url)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {suffix}")

    # Read file content
    content = await file.read()
    file_size = len(content)

    # Validate PDF magic bytes (%PDF-) only for PDF uploads
    if suffix == ".pdf":
        pdf_magic = b"%PDF-"
        if len(content) < 5 or content[:5] != pdf_magic:
            raise HTTPException(400, "Invalid PDF file: file does not have valid PDF header")

    # Generate unique object name
    file_hash = hashlib.md5(content).hexdigest()
    object_name = f"{file_hash}/{file.filename}"

    # Upload to MinIO
    content_type = "application/pdf" if suffix == ".pdf" else "application/octet-stream"
    minio_client.put_object(
        MINIO_BUCKET,
        object_name,
        BytesIO(content),
        length=file_size,
        content_type=content_type
    )

    # Use minio:// URI as filepath
    minio_path = f"minio://{MINIO_BUCKET}/{object_name}"

    workflow_id = get_workflow_id(minio_path)
    document_id = file_hash
    canonical_document_id = file_hash

    # Reuse only when SQLite still tracks this workflow.
    # If SQLite was purged, avoid returning stale Temporal state and create a fresh run ID.
    existing_doc = db.get_document(workflow_id)
    if existing_doc:
        existing_doc = assert_document_instance_access(user, existing_doc)
        try:
            handle = temporal_client.get_workflow_handle(workflow_id)
            state = await handle.query("get_state")
            if state:
                return DocumentSummary(
                    document_id=document_id,
                    canonical_document_id=canonical_document_id,
                    workflow_id=workflow_id,
                    filename=file.filename,
                    source_filename=file.filename,
                    source_file_fingerprint=file_hash,
                    authoritative=bool(existing_doc.get("source_manifest_name")) if existing_doc else False,
                    instance=normalize_instance(existing_doc.get("instance")),
                    stage=DocumentStage(state.get("stage", "registered")),
                    page_count=state.get("page_count", 0),
                    chunk_count=state.get("chunk_count", 0),
                    error_message=state.get("error_message"),
                )
        except HTTPException:
            raise
        except Exception:
            pass
    else:
        workflow_id = _rerun_workflow_id(workflow_id)

    # Start new workflow
    handle = await temporal_client.start_workflow(
        DocumentPipelineWorkflow.run,
        args=[
            document_id,
            file.filename,
            minio_path,
            chunk_size,
            chunk_overlap,
            min_tokens,
            marqo_url,
            index_name,
            auto_approve,
            stop_after_ocr,
        ],
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )

    # Save to SQLite for visibility during processing
    db.upsert_document(
        workflow_id=workflow_id,
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        filename=file.filename,
        source_filename=file.filename,
        source_file_fingerprint=file_hash,
        filepath=minio_path,
        stage="registered",
        stop_after_ocr=stop_after_ocr,
        instance=create_instance,
    )
    job_id = db.create_document_job(
        workflow_id=workflow_id,
        job_type="ocr_only" if stop_after_ocr else "pipeline",
        temporal_workflow_id=workflow_id,
        status="running",
        current_stage="registered",
        config={
            "auto_approve": auto_approve,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "min_tokens": min_tokens,
            "index_name": index_name,
            "stop_after_ocr": stop_after_ocr,
        },
    )
    original_artifact_id = db.add_document_artifact(
        workflow_id=workflow_id,
        job_id=job_id,
        artifact_type="original_upload",
        stage="registered",
        storage_uri=minio_path,
        mime_type=content_type,
        filename=file.filename,
        size_bytes=file_size,
        metadata={"uploaded_via": "upload_endpoint"},
    )
    source_type = "spreadsheet" if suffix in {".csv", ".xlsx"} else "document"
    canonical_input_type = "spreadsheet" if suffix in {".csv", ".xlsx"} else "pdf"
    db.update_document_fields(
        workflow_id,
        latest_job_id=job_id,
        original_artifact_id=original_artifact_id,
        source_type=source_type,
        canonical_input_type=canonical_input_type,
        stop_after_ocr=1 if stop_after_ocr else 0,
    )

    return DocumentSummary(
        document_id=document_id,
        canonical_document_id=canonical_document_id,
        workflow_id=workflow_id,
        filename=file.filename,
        source_filename=file.filename,
        source_file_fingerprint=file_hash,
        authoritative=False,
        instance=create_instance,
        stage=DocumentStage.REGISTERED,
        page_count=0,
        chunk_count=0,
    )


@app.post("/documents/batch", response_model=list[DocumentSummary])
@limiter.limit("5/minute")  # Stricter limit for batch operations
async def start_batch_workflows(
    request: Request,  # Required for rate limiting
    data: RegisterFolderRequest,
    user: RequireUpload,
    auto_approve: bool = False,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
    stop_after_ocr: bool = False,
    instance: str = "",
):
    """Start workflows for all supported documents in a directory."""
    create_instance = _resolve_create_instance(user, instance)
    directory = Path(data.directory)
    if not directory.exists():
        raise HTTPException(404, f"Directory not found: {data.directory}")

    candidate_files = [p for p in directory.glob("*") if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS]
    if not candidate_files:
        raise HTTPException(400, "No supported files found")

    results = []
    for pdf_path in candidate_files:
        try:
            result = await start_document_workflow(
                request,
                RegisterRequest(filepath=str(pdf_path)),
                user,
                auto_approve=auto_approve,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                min_tokens=min_tokens,
                stop_after_ocr=stop_after_ocr,
                instance=create_instance,
            )
            results.append(result)
        except Exception as e:
            # Log full error, return sanitized message
            logging.error(f"Batch workflow error for {pdf_path.name}: {str(e)}")
            results.append(DocumentSummary(
                document_id=hashlib.md5(str(pdf_path).encode()).hexdigest(),
                workflow_id=get_workflow_id(str(pdf_path)),
                filename=pdf_path.name,
                authoritative=False,
                stage=DocumentStage.FAILED,
                page_count=0,
                chunk_count=0,
                error_message="Failed to start workflow",
            ))

    return results


@app.get("/documents", response_model=list[DocumentSummary])
async def list_documents(
    user: CurrentUser,
    stage: Optional[DocumentStage] = None,
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """
    List all document workflows.

    Uses SQLite for fast listing (no Temporal queries for performance).
    Demo documents are excluded by default - use X-Include-Demo: true header to show them.
    Disabled (soft-deleted) documents are excluded by default - use X-Include-Disabled: true to show them.
    Results are limited to instances the caller can access.

    Pagination:
    - limit: Max documents to return (default 100, max 500)
    - offset: Skip first N documents (default 0)
    """
    stage_filter = stage.value if stage else None
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"

    # Use SQLite only for fast listing - no Temporal queries
    docs = db.list_documents(
        stage=stage_filter,
        limit=limit,
        offset=offset,
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=_instance_scope_for_user(user),
    )

    return [_document_summary_from_row(doc) for doc in docs]


@app.get("/documents/summary", response_model=DocumentCohortsResponse)
async def get_documents_summary(
    user: CurrentUser,
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """Return aggregate SQLite counts for dashboard totals and migration planning."""
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    summary = db.get_document_summary_counts(
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=_instance_scope_for_user(user),
    )
    return {
        **summary,
        "by_stage": {
            "ocr_review": summary.get("ocr_review_documents", 0),
            "translation_review": summary.get("translation_review_documents", 0),
            "chunk_review": summary.get("chunk_review_documents", 0),
            "translation_processing": summary.get("translation_processing_documents", 0),
            "chunking": summary.get("chunking_documents", 0),
            "ready_for_ingestion": summary.get("ready_for_ingestion_documents", 0),
            "failed": summary.get("failed_documents", 0),
        },
    }


@app.get("/documents/cohorts", response_model=DocumentCohortsResponse)
async def get_document_cohorts(
    user: CurrentUser,
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled")
):
    """Return machine-friendly cohort counts for queueing and orchestration."""
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    summary = db.get_document_summary_counts(
        include_demo=include_demo,
        include_disabled=include_disabled,
        instances=_instance_scope_for_user(user),
    )
    return {
        **summary,
        "by_stage": {
            "ocr_review": summary.get("ocr_review_documents", 0),
            "translation_review": summary.get("translation_review_documents", 0),
            "chunk_review": summary.get("chunk_review_documents", 0),
            "translation_processing": summary.get("translation_processing_documents", 0),
            "chunking": summary.get("chunking_documents", 0),
            "ready_for_ingestion": summary.get("ready_for_ingestion_documents", 0),
            "failed": summary.get("failed_documents", 0),
        },
    }


@app.get("/operations/queue", response_model=OperationQueueResponse)
async def get_operations_queue(
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled"),
):
    """Return documents that currently need operator or agent action."""
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    rows, total = db.list_operations_queue(
        limit=limit,
        offset=offset,
        include_demo=include_demo,
        include_disabled=include_disabled,
    )
    items = [
        OperationQueueEntry(
            workflow_id=row["workflow_id"],
            filename=row["filename"],
            stage=row["stage"],
            job_id=row.get("job_id"),
            job_type=row.get("job_type"),
            job_status=row.get("job_status"),
            started_at=row.get("started_at"),
            error_message=row.get("error_message") or row.get("reindex_reason"),
            available_actions=_list_available_actions(row, row),
        )
        for row in rows
    ]
    return OperationQueueResponse(items=items, total=total)


@app.get("/runs")
async def list_runs(
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    status: Optional[str] = None,
):
    """List recent document jobs across the system."""
    return db.list_runs(limit=limit, offset=offset, status=status)


@app.get("/runs/{job_id}")
async def get_run(job_id: int):
    """Get a specific document job/run."""
    run = db.get_document_job(job_id)
    if not run:
        raise HTTPException(404, f"Run not found: {job_id}")
    return run


def _build_document_detail(doc: dict) -> DocumentDetail:
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
        available_actions=_list_available_actions(doc, current_job),
        translated_count=sum(1 for p in db.get_pages(workflow_id) if p.get("translated_markdown")),
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


def _build_stage_io_payload(workflow_id: str, current_stage: Optional[str] = None) -> dict:
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

    input_types = {"original_upload", "normalized_pdf", "normalized_spreadsheet"}
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
        bucket = "input_artifacts" if artifact["artifact_type"] in input_types else "output_artifacts"
        grouped[stage][bucket].append(artifact)

    return {
        "workflow_id": workflow_id,
        "current_stage": current_stage,
        "stages": list(grouped.values()),
    }


async def _get_runtime_payload(workflow_id: str, doc: Optional[dict] = None) -> dict:
    doc = doc or db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")
    current_job = db.get_latest_document_job(workflow_id)
    runtime_workflow_id = (
        current_job.get("temporal_workflow_id")
        if current_job and current_job.get("status") == "running" and current_job.get("temporal_workflow_id")
        else workflow_id
    )

    chunking_progress = None
    if current_job and current_job.get("config_json"):
        try:
            parsed_config = json.loads(current_job["config_json"]) if isinstance(current_job["config_json"], str) else current_job["config_json"]
            if isinstance(parsed_config, dict):
                chunking_progress = parsed_config.get("chunking_progress")
        except Exception:
            chunking_progress = None

    runtime = {
        "workflow_id": workflow_id,
        "sqlite_stage": doc.get("stage"),
        "sqlite_error_message": doc.get("error_message"),
        "temporal_connected": temporal_client is not None,
        "job": current_job,
        "chunking_progress": chunking_progress,
        "temporal": None,
    }

    if temporal_client is None:
        return runtime

    try:
        handle = temporal_client.get_workflow_handle(runtime_workflow_id)
        description = await handle.describe()
        temporal_state = None
        query_error = None
        try:
            temporal_state = await handle.query("get_state")
        except Exception as exc:
            query_error = str(exc)

        runtime["temporal"] = {
            "workflow_id": runtime_workflow_id,
            "run_id": description.run_id,
            "status": description.status.name,
            "close_time": description.close_time.isoformat() if description.close_time else None,
            "execution_time": description.execution_time.isoformat() if description.execution_time else None,
            "state": temporal_state,
            "query_error": query_error,
        }
    except Exception as exc:
        runtime["temporal"] = {
            "workflow_id": workflow_id,
            "status": "UNAVAILABLE",
            "error": str(exc),
        }

    return runtime


@app.get("/documents/{workflow_id}", response_model=DocumentDetail)
async def get_document(workflow_id: str, user: CurrentUser):
    """Get document workflow state with artifacts and indexing metadata."""
    doc = _require_document_for_user(workflow_id, user)
    return _build_document_detail(doc)


@app.get("/documents/{workflow_id}/error-details")
async def get_workflow_error_details(workflow_id: str):
    """
    Get detailed error information from Temporal for a failed workflow.
    
    Returns comprehensive error details including:
    - Error message
    - Stack trace (if available)
    - Failure type
    - Workflow execution status
    
    This endpoint queries Temporal directly to get the most detailed
    error information available, which may be more complete than what's
    stored in SQLite.
    """
    import traceback
    
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        description = await handle.describe()
        
        result = {
            "workflow_id": workflow_id,
            "run_id": description.run_id,
            "status": description.status.name,
            "error_message": None,
            "error_type": None,
            "stack_trace": None,
            "has_error": False
        }
        
        # If workflow is failed, try to get detailed error information
        if description.status.name == "FAILED":
            result["has_error"] = True
            
            # Try to get error from workflow result (this raises WorkflowFailureError for failed workflows)
            try:
                await handle.result()
            except WorkflowFailureError as wf_err:
                # Extract error details from the failure
                result["error_message"] = str(wf_err)
                result["error_type"] = type(wf_err).__name__
                
                # Try to get the underlying cause
                if hasattr(wf_err, 'cause') and wf_err.cause:
                    cause = wf_err.cause
                    result["error_message"] = str(cause)
                    result["error_type"] = type(cause).__name__
                    
                    # Get stack trace if available
                    if hasattr(cause, '__traceback__') and cause.__traceback__:
                        result["stack_trace"] = ''.join(traceback.format_tb(cause.__traceback__))
                    elif hasattr(wf_err, '__traceback__') and wf_err.__traceback__:
                        result["stack_trace"] = ''.join(traceback.format_tb(wf_err.__traceback__))
                
                # Also try to get failure details from the exception itself
                if hasattr(wf_err, 'failure') and wf_err.failure:
                    failure = wf_err.failure
                    if hasattr(failure, 'message') and failure.message:
                        result["error_message"] = failure.message
                    if hasattr(failure, 'stack_trace') and failure.stack_trace:
                        result["stack_trace"] = failure.stack_trace
            except Exception as e:
                # If result() doesn't work, try other methods
                result["error_message"] = f"Could not retrieve error details: {str(e)}"
        
        # Also try to get error from workflow state query (fallback)
        if not result["error_message"]:
            try:
                state = await handle.query("get_state")
                if state and state.get("error_message"):
                    result["error_message"] = state.get("error_message")
                    result["has_error"] = True
            except Exception:
                pass  # Workflow might not support queries or be in wrong state
        
        return result
        
    except Exception as e:
        # If workflow doesn't exist or can't be accessed
        error_msg = str(e)
        if "not found" in error_msg.lower() or "workflow" in error_msg.lower():
            raise HTTPException(404, f"Workflow not found: {workflow_id}")
        raise HTTPException(500, f"Error fetching workflow details: {error_msg}")


@app.get("/documents/{workflow_id}/runtime")
async def get_document_runtime(workflow_id: str):
    """Return live runtime status by combining SQLite state and Temporal workflow state."""
    doc = db.get_document(workflow_id)
    return await _get_runtime_payload(workflow_id, doc=doc)


@app.get("/documents/{workflow_id}/artifacts")
async def list_document_artifacts(workflow_id: str):
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")
    return db.list_document_artifacts(workflow_id)


@app.get("/documents/{workflow_id}/artifacts/{artifact_id}")
async def get_document_artifact(workflow_id: str, artifact_id: int):
    artifact = db.get_document_artifact(workflow_id, artifact_id)
    if not artifact:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")
    return artifact


@app.get("/documents/{workflow_id}/artifacts/{artifact_id}/content")
async def get_document_artifact_content(workflow_id: str, artifact_id: int):
    artifact = db.get_document_artifact(workflow_id, artifact_id)
    if not artifact:
        raise HTTPException(404, f"Artifact not found: {artifact_id}")

    storage_uri = artifact["storage_uri"]
    if storage_uri.startswith("minio://"):
        path = storage_uri.replace("minio://", "")
        bucket, object_name = path.split("/", 1)
        response = minio_client.get_object(bucket, object_name)
        return StreamingResponse(
            response,
            media_type=artifact.get("mime_type") or "application/octet-stream",
            headers={"Content-Disposition": f'inline; filename="{artifact.get("filename") or "artifact"}"'},
        )

    file_path = Path(storage_uri)
    if not file_path.exists():
        raise HTTPException(404, "Artifact content not found")
    return StreamingResponse(
        open(file_path, "rb"),
        media_type=artifact.get("mime_type") or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{artifact.get("filename") or file_path.name}"'},
    )


@app.get("/documents/{workflow_id}/jobs")
async def list_document_jobs(workflow_id: str, limit: int = Query(20, le=100)):
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")
    return db.list_document_jobs(workflow_id, limit=limit)


@app.get("/documents/{workflow_id}/stage-io")
async def get_document_stage_io(workflow_id: str):
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")
    return _build_stage_io_payload(workflow_id, current_stage=doc.get("stage"))


@app.get("/documents/{workflow_id}/allowed-actions")
async def get_document_allowed_actions(workflow_id: str):
    """Return the currently valid machine-facing actions for a document."""
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")
    return {
        "workflow_id": workflow_id,
        "stage": doc.get("stage"),
        "reindex_required": bool(doc.get("reindex_required")),
        "available_actions": _list_available_actions(doc, db.get_latest_document_job(workflow_id)),
    }


@app.get("/documents/{workflow_id}/graph", response_model=DocumentGraph)
async def get_document_graph(workflow_id: str):
    """Return a document-centric graph of state, jobs, artifacts, index status, and runtime."""
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")
    detail = _build_document_detail(doc)
    return DocumentGraph(
        workflow_id=workflow_id,
        document=detail,
        jobs=db.list_document_jobs(workflow_id, limit=100),
        artifacts=detail.artifacts,
        index_status=detail.index_status,
        stage_io=_build_stage_io_payload(workflow_id, current_stage=doc.get("stage")),
        runtime=await _get_runtime_payload(workflow_id, doc=doc),
    )


@app.delete("/documents/{workflow_id}")
async def disable_document(
    workflow_id: str,
    user: RequireAdmin,
    remove_from_search: bool = Query(True),
):
    """
    Soft delete a document (disable it).

    This performs a soft delete:
    - Marks the document as disabled in SQLite (hidden from list by default)
    - Optionally removes all chunks from Marqo search index
    - Cancels the workflow if still running

    The document can be restored by calling POST /documents/{id}/restore.
    Use X-Include-Disabled: true header in list_documents to see disabled documents.

    Args:
        workflow_id: The document workflow ID
        remove_from_search: If True (default), removes chunks from Marqo index
    Requires permission: admin.
    """
    doc = _require_document_for_user(workflow_id, user)

    result = {
        "workflow_id": workflow_id,
        "disabled": True,
        "workflow_cancelled": False,
        "marqo_deleted": 0
    }

    # Try to cancel workflow if still running
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        await handle.cancel()
        result["workflow_cancelled"] = True
    except Exception:
        pass  # Workflow already completed/cancelled

    # Mark as disabled in SQLite
    db.set_document_disabled(workflow_id, True)

    # Remove from Marqo if requested
    if remove_from_search:
        doc_id = doc.get("document_id")
        if doc_id:
            marqo_result = delete_chunks_from_marqo(doc_id)
            result["marqo_deleted"] = marqo_result.get("deleted", 0)
            if "error" in marqo_result:
                result["marqo_error"] = marqo_result["error"]

    # Log audit
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", ""),
        action_type="disable_document",
        metadata={"remove_from_search": remove_from_search, "marqo_deleted": result["marqo_deleted"]}
    )

    return result


@app.post("/documents/{workflow_id}/restore")
async def restore_document(workflow_id: str, user: RequireAdmin):
    """
    Restore a soft-deleted (disabled) document.

    Note: This only restores the document in SQLite. Chunks that were removed
    from Marqo will NOT be automatically re-indexed. To re-index, you would
    need to re-run the ingestion process.
    """
    doc = _require_document_for_user(workflow_id, user)

    db.set_document_disabled(workflow_id, False)

    # Log audit
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", ""),
        action_type="restore_document"
    )

    return {"workflow_id": workflow_id, "restored": True}


@app.post("/documents/{workflow_id}/reingest")
async def reingest_document(
    workflow_id: str,
    user: RequirePipeline,
    marqo_url: str = "",
    index_name: str = "documents-index",
):
    """
    Re-ingest a completed document to Marqo.

    Use this to re-ingest documents that completed but weren't properly
    indexed (e.g., due to index schema changes). This starts a lightweight
    workflow that uses chunks already stored in SQLite.

    The document must have chunks stored in SQLite (typically from a
    completed or previously ingested document).
    Client-supplied marqo_url is ignored; ingest uses MARQO_URL from the environment.
    """
    marqo_url = _ignore_client_marqo_url(marqo_url)
    # Get document from SQLite
    doc = _require_document_for_user(workflow_id, user)

    # Get chunks from SQLite
    chunks = db.get_chunks(workflow_id, include_excluded=False)
    if not chunks:
        raise HTTPException(400, f"No chunks found for document. The document may need to be reprocessed from scratch.")

    document_id = doc.get("document_id", "")
    filename = doc.get("filename", "")
    page_count = doc.get("page_count", 0)

    # Generate unique workflow ID for re-ingestion
    import time
    reingest_workflow_id = f"{workflow_id}-reingest-{int(time.time())}"

    # Start re-ingestion workflow
    await temporal_client.start_workflow(
        ReingestionWorkflow.run,
        args=[
            document_id,
            filename,
            workflow_id,  # original workflow_id for SQLite updates
            page_count,
            len(chunks),
            marqo_url,
            index_name
        ],
        id=reingest_workflow_id,
        task_queue=TASK_QUEUE,
    )
    db.create_document_job(
        workflow_id=workflow_id,
        job_type="reingest",
        temporal_workflow_id=reingest_workflow_id,
        status="running",
        current_stage="ingesting",
        config={"index_name": index_name, "chunk_count": len(chunks), "marqo_url": marqo_url or None},
    )

    # Log audit
    db.log_audit(
        workflow_id=workflow_id,
        document_id=document_id,
        action_type="reingest_started",
        metadata={"reingest_workflow_id": reingest_workflow_id, "chunk_count": len(chunks)}
    )

    return {
        "workflow_id": workflow_id,
        "reingest_workflow_id": reingest_workflow_id,
        "chunk_count": len(chunks),
        "status": "started"
    }


@app.post("/documents/{workflow_id}/retry-ingestion")
async def retry_ingestion(
    workflow_id: str,
    user: RequirePipeline,
    marqo_url: str = "",
    index_name: str = "documents-index",
):
    """Alias for reingesting a document when search is stale or missing."""
    return await reingest_document(
        workflow_id,
        user=user,
        marqo_url=_ignore_client_marqo_url(marqo_url),
        index_name=index_name,
    )


@app.post("/documents/{workflow_id}/retry-ocr")
async def retry_ocr(workflow_id: str, user: RequirePipeline):
    """Retry OCR for an existing document and stop at OCR review."""
    doc = _require_document_for_user(workflow_id, user)
    filepath = doc.get("filepath")
    if not filepath:
        raise HTTPException(400, "Document has no source filepath for OCR retry")
    temporal_workflow_id = f"{workflow_id}-retry-ocr-{int(datetime.utcnow().timestamp())}"
    await temporal_client.start_workflow(
        OcrOnlyWorkflow.run,
        args=[workflow_id, doc["document_id"], doc["filename"], filepath],
        id=temporal_workflow_id,
        task_queue=TASK_QUEUE,
    )
    job_id = db.create_document_job(
        workflow_id=workflow_id,
        job_type="ocr_retry",
        temporal_workflow_id=temporal_workflow_id,
        status="running",
        current_stage="ocr_processing",
        config={"source": "api_retry_ocr"},
    )
    db.update_document_fields(workflow_id, latest_job_id=job_id, error_message=None)
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", workflow_id),
        action_type="retry_ocr",
        metadata={"temporal_workflow_id": temporal_workflow_id},
    )
    return {"workflow_id": workflow_id, "status": "started", "retry_workflow_id": temporal_workflow_id}


@app.post("/documents/{workflow_id}/retry-translation")
async def retry_translation(workflow_id: str, user: RequirePipeline):
    """Retry translation for an existing document and stop at translation review."""
    doc = _require_document_for_user(workflow_id, user)
    if not db.get_pages(workflow_id):
        raise HTTPException(400, "No OCR pages found for translation retry")
    temporal_workflow_id = f"{workflow_id}-retry-translation-{int(datetime.utcnow().timestamp())}"
    await temporal_client.start_workflow(
        TranslationOnlyWorkflow.run,
        args=[workflow_id, doc["document_id"], doc["filename"]],
        id=temporal_workflow_id,
        task_queue=TASK_QUEUE,
    )
    job_id = db.create_document_job(
        workflow_id=workflow_id,
        job_type="translation_retry",
        temporal_workflow_id=temporal_workflow_id,
        status="running",
        current_stage="translation_processing",
        config={"source": "api_retry_translation"},
    )
    db.update_document_fields(workflow_id, latest_job_id=job_id, error_message=None)
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", workflow_id),
        action_type="retry_translation",
        metadata={"temporal_workflow_id": temporal_workflow_id},
    )
    return {"workflow_id": workflow_id, "status": "started", "retry_workflow_id": temporal_workflow_id}


@app.post("/documents/{workflow_id}/retry-chunking")
async def retry_chunking(
    workflow_id: str,
    user: RequirePipeline,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
):
    """Retry chunking for an existing document and stop at chunk review."""
    doc = _require_document_for_user(workflow_id, user)
    if not db.get_pages(workflow_id):
        raise HTTPException(400, "No page content found for chunking retry")
    temporal_workflow_id = f"{workflow_id}-retry-chunking-{int(datetime.utcnow().timestamp())}"
    await temporal_client.start_workflow(
        ChunkingOnlyWorkflow.run,
        args=[
            workflow_id,
            doc["document_id"],
            doc["filename"],
            doc.get("page_count", 0),
            chunk_size,
            chunk_overlap,
            min_tokens,
        ],
        id=temporal_workflow_id,
        task_queue=TASK_QUEUE,
    )
    job_id = db.create_document_job(
        workflow_id=workflow_id,
        job_type="chunking_retry",
        temporal_workflow_id=temporal_workflow_id,
        status="running",
        current_stage="chunking",
        config={
            "source": "api_retry_chunking",
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "min_tokens": min_tokens,
        },
    )
    db.update_document_fields(workflow_id, latest_job_id=job_id, error_message=None)
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", workflow_id),
        action_type="retry_chunking",
        metadata={"temporal_workflow_id": temporal_workflow_id},
    )
    return {"workflow_id": workflow_id, "status": "started", "retry_workflow_id": temporal_workflow_id}


@app.post("/documents/{workflow_id}/mark-reindex-required")
async def mark_reindex_required(workflow_id: str, payload: ReindexStateRequest, user: RequirePipeline):
    """Mark a document as needing reindex after chunk edits or operational drift."""
    _require_document_for_user(workflow_id, user)
    updated = _mark_reindex_required(
        workflow_id,
        payload.reason or "Marked manually for reindex",
        metadata={"source": "api"},
    )
    return {
        "workflow_id": workflow_id,
        "reindex_required": bool(updated.get("reindex_required")) if updated else True,
        "reindex_reason": updated.get("reindex_reason") if updated else payload.reason,
    }


@app.post("/documents/{workflow_id}/clear-reindex-required")
async def clear_reindex_required(workflow_id: str, user: RequirePipeline):
    """Clear the reindex-required flag after verification or reingestion."""
    doc = _require_document_for_user(workflow_id, user)
    old_reason = doc.get("reindex_reason")
    updated = db.mark_document_reindex_required(workflow_id, False)
    db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", workflow_id),
        action_type="clear_reindex_required",
        field_name="reindex_required",
        old_value="true",
        new_value="false",
        metadata={"reason": old_reason},
    )
    return {
        "workflow_id": workflow_id,
        "reindex_required": bool(updated.get("reindex_required")) if updated else False,
        "reindex_reason": updated.get("reindex_reason") if updated else None,
    }


@app.post("/documents/{workflow_id}/demo")
async def set_document_demo(workflow_id: str, user: RequireAdmin, is_demo: bool = Query(True)):
    """
    Mark a document as demo.

    Demo documents are excluded from the UI by default but always available
    for API testing via include_demo=true parameter.
    """
    _require_document_for_user(workflow_id, user)
    db.set_document_demo(workflow_id, is_demo)
    return {"workflow_id": workflow_id, "is_demo": is_demo}


async def _reconcile_single_document(doc: dict) -> dict:
    workflow_id = doc.get("workflow_id")
    current_stage = doc.get("stage")
    materialized = db.reconcile_materialized_state(workflow_id)
    if materialized and materialized.get("updated"):
        doc = db.get_document(workflow_id) or doc
        current_stage = doc.get("stage")
        return {
            "workflow_id": workflow_id,
            "action": "materialized_state_reconciled",
            "to": current_stage,
            "page_count": doc.get("page_count", 0),
            "chunk_count": doc.get("chunk_count", 0),
            "job_status": materialized.get("job_status"),
            "job_stage": materialized.get("job_stage"),
        }

    current_job = db.get_latest_document_job(workflow_id)
    runtime_workflow_id = (
        current_job.get("temporal_workflow_id")
        if current_job and current_job.get("status") == "running" and current_job.get("temporal_workflow_id")
        else workflow_id
    )

    try:
        handle = temporal_client.get_workflow_handle(runtime_workflow_id)
        state = await asyncio.wait_for(
            handle.query("get_state"),
            timeout=5.0,
        )
        temporal_stage = state.get("stage") if state else None
        if temporal_stage and temporal_stage != current_stage:
            db.update_document_stage(workflow_id, temporal_stage)
            return {
                "workflow_id": workflow_id,
                "action": "stage_synced",
                "from": current_stage,
                "to": temporal_stage,
                "temporal_workflow_id": runtime_workflow_id,
            }
        return {
            "workflow_id": workflow_id,
            "action": "no_change",
            "stage": current_stage,
            "temporal_workflow_id": runtime_workflow_id,
        }
    except asyncio.TimeoutError:
        db.update_document_stage(workflow_id, "failed", error_message="Workflow query timed out")
        return {
            "workflow_id": workflow_id,
            "action": "marked_failed",
            "from": current_stage,
            "reason": "query_timeout",
        }
    except Exception as exc:
        error_msg = str(exc)
        if "not found" in error_msg.lower() or "workflow task" in error_msg.lower():
            db.update_document_stage(workflow_id, "failed", error_message="Workflow terminated or lost")
            db.log_audit(
                workflow_id=workflow_id,
                document_id=doc.get("document_id", ""),
                action_type="reconcile_failed",
                metadata={"from_stage": current_stage, "reason": "workflow_not_found"},
            )
            return {
                "workflow_id": workflow_id,
                "action": "marked_failed",
                "from": current_stage,
                "reason": "workflow_not_found",
            }
        return {
            "workflow_id": workflow_id,
            "action": "error",
            "from": current_stage,
            "reason": error_msg,
        }


@app.post("/documents/{workflow_id}/reconcile")
async def reconcile_single_document(workflow_id: str, user: RequirePipeline):
    """Reconcile SQLite stage with Temporal state for one document."""
    doc = _require_document_for_user(workflow_id, user)
    return await _reconcile_single_document(doc)


# =============================================================================
# Approval Routes
# =============================================================================

async def _validate_approval_stage(workflow_id: str, expected_stage: str):
    """Validate that workflow is in the expected stage before approval."""
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        state = await handle.query("get_state")
        current_stage = state.get("stage") if isinstance(state, dict) else getattr(state, "stage", None)
        if current_stage != expected_stage:
            raise HTTPException(
                400,
                f"Cannot approve: workflow is in '{current_stage}' stage, expected '{expected_stage}'"
            )
        return handle
    except HTTPException:
        raise
    except Exception as e:
        # Try SQLite fallback to check if workflow exists but is completed/failed
        doc = db.get_document(workflow_id)
        if doc:
            raise HTTPException(
                400,
                f"Cannot approve: workflow is in '{doc.get('stage')}' stage (completed/failed workflows cannot be approved)"
            )
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


async def _execute_bulk_approval_action(
    request: BulkWorkflowActionRequest,
    action: str,
    expected_stage: str,
    signal_method,
    user: AuthUser,
) -> BulkWorkflowActionResponse:
    results: list[BulkWorkflowActionResult] = []
    for workflow_id in request.workflow_ids:
        doc = _document_for_user_or_none(workflow_id, user)
        if not doc:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=False,
                action=action,
                message="document_not_found",
            ))
            continue
        current_stage = doc.get("stage")
        if current_stage != expected_stage:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=False,
                action=action,
                message=f"invalid_stage:{current_stage}",
            ))
            continue
        if request.dry_run:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=True,
                action=action,
                message="would_execute",
            ))
            continue
        try:
            handle = await _validate_approval_stage(workflow_id, expected_stage)
            await handle.signal(signal_method)
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=True,
                action=action,
                message="queued",
            ))
        except Exception as exc:
            results.append(BulkWorkflowActionResult(
                workflow_id=workflow_id,
                ok=False,
                action=action,
                message=str(exc),
            ))

    return BulkWorkflowActionResponse(
        action=action,
        dry_run=request.dry_run,
        requested=len(request.workflow_ids),
        succeeded=sum(1 for result in results if result.ok),
        failed=sum(1 for result in results if not result.ok),
        results=results,
    )


@app.post("/documents/bulk/approve-ocr", response_model=BulkWorkflowActionResponse)
async def bulk_approve_ocr(request: BulkWorkflowActionRequest, user: RequireReview):
    """Bulk-approve documents waiting in OCR review."""
    return await _execute_bulk_approval_action(
        request,
        action="approve_ocr",
        expected_stage="ocr_review",
        signal_method=DocumentPipelineWorkflow.approve_ocr,
        user=user,
    )


@app.post("/documents/bulk/approve-translation", response_model=BulkWorkflowActionResponse)
async def bulk_approve_translation(request: BulkWorkflowActionRequest, user: RequireReview):
    """Bulk-approve documents waiting in translation review."""
    return await _execute_bulk_approval_action(
        request,
        action="approve_translation",
        expected_stage="translation_review",
        signal_method=DocumentPipelineWorkflow.approve_translation,
        user=user,
    )


@app.post("/documents/bulk/approve-chunks", response_model=BulkWorkflowActionResponse)
async def bulk_approve_chunks(request: BulkWorkflowActionRequest, user: RequireReview):
    """Bulk-approve documents waiting in chunk review."""
    return await _execute_bulk_approval_action(
        request,
        action="approve_chunks",
        expected_stage="chunk_review",
        signal_method=DocumentPipelineWorkflow.approve_chunks,
        user=user,
    )


@app.post("/documents/bulk/reindex", response_model=BulkWorkflowActionResponse)
async def bulk_reindex_documents(
    request: BulkWorkflowActionRequest,
    user: RequirePipeline,
    marqo_url: str = "",
    index_name: str = "documents-index",
):
    """Bulk queue reingestion for completed or dirty documents.

    Client-supplied marqo_url is ignored; ingest uses MARQO_URL from the environment.
    """
    marqo_url = _ignore_client_marqo_url(marqo_url)
    results: list[BulkWorkflowActionResult] = []
    for workflow_id in request.workflow_ids:
        doc = _document_for_user_or_none(workflow_id, user)
        if not doc:
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=False, action="reindex", message="document_not_found"))
            continue
        if doc.get("stage") not in {"completed", "ready_for_ingestion", "chunk_review"} and not doc.get("reindex_required"):
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=False, action="reindex", message=f"invalid_stage:{doc.get('stage')}"))
            continue
        if request.dry_run:
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=True, action="reindex", message="would_execute"))
            continue
        try:
            await reingest_document(workflow_id, user=user, marqo_url=marqo_url, index_name=index_name)
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=True, action="reindex", message="queued"))
        except Exception as exc:
            results.append(BulkWorkflowActionResult(workflow_id=workflow_id, ok=False, action="reindex", message=str(exc)))

    return BulkWorkflowActionResponse(
        action="reindex",
        dry_run=request.dry_run,
        requested=len(request.workflow_ids),
        succeeded=sum(1 for result in results if result.ok),
        failed=sum(1 for result in results if not result.ok),
        results=results,
    )


@app.post("/documents/{workflow_id}/approve-ocr")
async def approve_ocr(workflow_id: str, user: RequireReview):
    """Approve OCR results and continue to chunking. Requires permission: review."""
    _require_document_for_user(workflow_id, user)
    handle = await _validate_approval_stage(workflow_id, "ocr_review")
    await handle.signal(DocumentPipelineWorkflow.approve_ocr)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="ocr_approved",
        new_value=True,
        metadata={"stage": "ocr_review", "next_stage": "translation_processing"}
    )

    return {"approved": "ocr", "workflow_id": workflow_id}


@app.post("/documents/{workflow_id}/approve-chunks")
async def approve_chunks(workflow_id: str, user: RequireReview):
    """Approve chunks and continue to prepare for ingestion."""
    _require_document_for_user(workflow_id, user)
    handle = await _validate_approval_stage(workflow_id, "chunk_review")
    await handle.signal(DocumentPipelineWorkflow.approve_chunks)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="chunks_approved",
        new_value=True,
        metadata={"stage": "chunk_review", "next_stage": "ready_for_ingestion"}
    )

    return {"approved": "chunks", "workflow_id": workflow_id}


@app.post("/documents/{workflow_id}/approve-translation")
async def approve_translation(workflow_id: str, user: RequireReview):
    """Approve translations and continue to chunking."""
    _require_document_for_user(workflow_id, user)
    handle = await _validate_approval_stage(workflow_id, "translation_review")
    await handle.signal(DocumentPipelineWorkflow.approve_translation)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="translation_approved",
        new_value=True,
        metadata={"stage": "translation_review", "next_stage": "chunking"}
    )

    return {"approved": "translation", "workflow_id": workflow_id}


@app.post("/documents/{workflow_id}/approve-ingestion")
async def approve_ingestion(workflow_id: str, user: RequireReview):
    """Approve ingestion and continue to Marqo ingestion."""
    _require_document_for_user(workflow_id, user)
    handle = await _validate_approval_stage(workflow_id, "ready_for_ingestion")
    await handle.signal(DocumentPipelineWorkflow.approve_ingestion)

    # Log approval
    _log_audit(
        workflow_id=workflow_id,
        action_type="approval",
        entity_type="document",
        field_name="ingestion_approved",
        new_value=True,
        metadata={"stage": "ready_for_ingestion", "next_stage": "ingesting"}
    )

    return {"approved": "ingestion", "workflow_id": workflow_id}


# =============================================================================
# Audit Log Helper and Routes
# =============================================================================

def _log_audit(
    workflow_id: str,
    action_type: str,
    entity_type: str = None,
    entity_id: int = None,
    field_name: str = None,
    old_value = None,
    new_value = None,
    metadata: dict = None
):
    """
    Helper to log audit entries with JSON serialization.

    Args:
        workflow_id: The Temporal workflow ID
        action_type: Type of action (page_edit, chunk_edit, approval, etc.)
        entity_type: Type of entity (page, chunk)
        entity_id: Entity identifier (page_number, chunk_number)
        field_name: Name of the field changed
        old_value: Previous value (will be JSON serialized if not string)
        new_value: New value (will be JSON serialized if not string)
        metadata: Additional context as dict
    """
    # Get document_id from SQLite
    doc = db.get_document(workflow_id)
    document_id = doc["document_id"] if doc else workflow_id

    # Serialize values to JSON if needed
    old_str = json.dumps(old_value) if old_value is not None and not isinstance(old_value, str) else old_value
    new_str = json.dumps(new_value) if new_value is not None and not isinstance(new_value, str) else new_value

    db.log_audit(
        workflow_id=workflow_id,
        document_id=document_id,
        action_type=action_type,
        entity_type=entity_type,
        entity_id=entity_id,
        field_name=field_name,
        old_value=old_str,
        new_value=new_str,
        metadata=metadata
    )


@app.get("/audit", response_model=AuditLogResponse)
async def get_all_audit_logs(
    action_type: str = None,
    limit: int = Query(50, le=200),
    offset: int = 0
):
    """
    Get global audit trail across all documents.

    Returns a list of all changes including:
    - Stage transitions
    - Page edits
    - Chunk edits
    - Approvals
    - Resets

    Each entry includes the document filename for context.
    """
    logs = db.get_all_audit_logs(
        action_type=action_type,
        limit=limit,
        offset=offset
    )
    total = db.get_all_audit_log_count(action_type)

    return AuditLogResponse(
        logs=logs,
        total=total,
        limit=limit,
        offset=offset
    )


@app.get("/documents/{workflow_id}/audit", response_model=AuditLogResponse)
async def get_document_audit_log(
    workflow_id: str,
    action_type: str = None,
    limit: int = Query(50, le=200),
    offset: int = 0
):
    """
    Get audit trail for a document.

    Returns a list of all changes made to the document including:
    - Stage transitions
    - Page edits
    - Chunk edits
    - Approvals
    - Resets
    """
    logs = db.get_audit_logs(
        workflow_id=workflow_id,
        action_type=action_type,
        limit=limit,
        offset=offset
    )
    total = db.get_audit_log_count(workflow_id, action_type)

    return AuditLogResponse(
        logs=logs,
        total=total,
        limit=limit,
        offset=offset
    )


# =============================================================================
# Page Routes (OCR Review)
# =============================================================================

@app.get("/documents/{workflow_id}/pages")
async def list_pages(workflow_id: str):
    """Get all pages for a document. SQLite-first for speed."""
    # SQLite-first - instant response
    pages = db.get_pages(workflow_id)
    if pages:
        return pages

    # Document exists but no pages yet (still in early OCR stage)
    doc = db.get_document(workflow_id)
    if doc:
        return []  # Empty list, OCR not done yet

    raise HTTPException(404, f"Document not found: {workflow_id}")


@app.get("/documents/{workflow_id}/pages/{page_num}")
async def get_page(workflow_id: str, page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)")):
    """Get a specific page. SQLite-first for speed."""
    # SQLite-first - instant response
    page = db.get_page(workflow_id, page_num)
    if page:
        return page

    raise HTTPException(404, f"Page {page_num} not found")


@app.patch("/documents/{workflow_id}/pages/{page_num}")
async def update_page(
    workflow_id: str,
    data: PageUpdate,
    user: RequireReview,
    page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)"),
):
    """Update a page (edit markdown, mark reviewed)."""
    doc = _require_document_for_user(workflow_id, user)
    old_page = db.get_page(workflow_id, page_num)
    if not old_page:
        raise HTTPException(404, f"Page {page_num} not found")

    updated = db.update_page(
        workflow_id,
        page_num,
        edited_markdown=data.edited_markdown,
        is_reviewed=data.is_reviewed,
        reviewer_notes=data.reviewer_notes,
        edited_translation=data.edited_translation,
        translation_reviewed=data.translation_reviewed,
        translation_notes=data.translation_notes,
    )
    if not updated:
        raise HTTPException(404, f"Page {page_num} not found")

    # Log audits for changed fields
    if data.edited_markdown is not None:
        old_text = old_page.get("edited_markdown") or old_page.get("original_markdown", "")
        _log_audit(
            workflow_id=workflow_id,
            action_type="page_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="edited_markdown",
            old_value=old_text,
            new_value=data.edited_markdown
        )

    if data.is_reviewed is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="page_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="is_reviewed",
            old_value=old_page.get("is_reviewed", False),
            new_value=data.is_reviewed
        )

    if data.reviewer_notes is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="page_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="reviewer_notes",
            old_value=old_page.get("reviewer_notes"),
            new_value=data.reviewer_notes
        )

    if data.edited_translation is not None:
        old_translation = old_page.get("edited_translation") or old_page.get("translated_markdown", "")
        _log_audit(
            workflow_id=workflow_id,
            action_type="translation_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="edited_translation",
            old_value=old_translation,
            new_value=data.edited_translation
        )

    if data.translation_reviewed is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="translation_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="translation_reviewed",
            old_value=old_page.get("translation_reviewed", False),
            new_value=data.translation_reviewed
        )

    if data.translation_notes is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="translation_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="translation_notes",
            old_value=old_page.get("translation_notes"),
            new_value=data.translation_notes
        )

    # Review flags/notes alone must not dirty the search index — only content edits.
    content_changed = data.edited_markdown is not None or data.edited_translation is not None
    if doc and content_changed and (
        doc.get("chunk_count", 0) > 0
        or doc.get("stage") in {"chunking", "chunk_review", "ready_for_ingestion", "ingesting", "completed"}
    ):
        _mark_reindex_required(
            workflow_id,
            "Page content changed after chunk generation; rechunk and reindex required",
            metadata={"page_number": page_num},
        )

    return db.get_page(workflow_id, page_num)


@app.post("/documents/{workflow_id}/pages/{page_num}/reset")
async def reset_page(
    workflow_id: str,
    user: RequireReview,
    page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)"),
):
    """Reset page to original OCR output."""
    doc = _require_document_for_user(workflow_id, user)
    old_page = db.get_page(workflow_id, page_num)
    if not old_page:
        raise HTTPException(404, f"Page {page_num} not found")
    db.reset_page(workflow_id, page_num)

    # Log reset action
    _log_audit(
        workflow_id=workflow_id,
        action_type="page_reset",
        entity_type="page",
        entity_id=page_num,
        field_name="edited_markdown",
        old_value=old_page.get("edited_markdown") if old_page else None,
        new_value=None,
        metadata={"reset_to": "original_markdown"}
    )

    if doc and (doc.get("chunk_count", 0) > 0 or doc.get("stage") in {"chunking", "chunk_review", "ready_for_ingestion", "ingesting", "completed"}):
        _mark_reindex_required(
            workflow_id,
            "Page reset after chunk generation; rechunk and reindex required",
            metadata={"page_number": page_num},
        )

    return db.get_page(workflow_id, page_num)


# =============================================================================
# Chunk Routes (Chunk Review)
# =============================================================================

@app.get("/chunks/search")
async def search_chunks_across_documents(
    q: str = Query("", description="Keyword search within chunk text"),
    tags: Optional[list[str]] = Query(None, description="Repeatable dimension:value filter"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_excluded: bool = Query(False, description="Include excluded chunks"),
    stage: Optional[DocumentStage] = Query(None, description="Optional document stage filter"),
):
    """Search chunks across all documents for KB maintainer workflows."""
    chunks, total = db.search_chunks(
        query=q,
        tags=tags or [],
        limit=limit,
        offset=offset,
        include_excluded=include_excluded,
        stage=stage.value if stage else None,
    )
    return {
        "items": chunks,
        "total": total,
        "limit": limit,
        "offset": offset,
        "query": q,
        "tags": tags or [],
        "include_excluded": include_excluded,
        "stage": stage.value if stage else None,
    }


@app.get("/documents/{workflow_id}/chunks")
async def list_chunks(workflow_id: str, include_excluded: bool = False):
    """Get all chunks for a document. SQLite-first for speed."""
    # SQLite-first - instant response
    chunks = db.get_chunks(workflow_id, include_excluded=include_excluded)
    if chunks:
        return chunks

    # Document exists but no chunks yet (still in early stages)
    doc = db.get_document(workflow_id)
    if doc:
        return []  # Empty list, chunking not done yet

    raise HTTPException(404, f"Document not found: {workflow_id}")


@app.get("/documents/{workflow_id}/chunks/{chunk_num}")
async def get_chunk(workflow_id: str, chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)")):
    """Get a specific chunk. SQLite-first for speed."""
    # SQLite-first - instant response
    chunk = db.get_chunk(workflow_id, chunk_num)
    if chunk:
        return chunk

    raise HTTPException(404, f"Chunk {chunk_num} not found")


@app.patch("/documents/{workflow_id}/chunks/{chunk_num}")
async def update_chunk(
    workflow_id: str,
    data: ChunkUpdate,
    user: RequireReview,
    chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)"),
):
    """Update a chunk (edit text, mark reviewed, exclude)."""
    doc = _require_document_for_user(workflow_id, user)
    old_chunk = db.get_chunk(workflow_id, chunk_num)
    if not old_chunk:
        raise HTTPException(404, f"Chunk {chunk_num} not found")

    updated = db.update_chunk(
        workflow_id,
        chunk_num,
        edited_text=data.edited_text,
        is_reviewed=data.is_reviewed,
        is_excluded=data.is_excluded,
        reviewer_notes=data.reviewer_notes,
    )
    if not updated:
        raise HTTPException(404, f"Chunk {chunk_num} not found")

    # Log audits for changed fields
    if data.edited_text is not None:
        old_text = old_chunk.get("edited_text") or old_chunk.get("original_text", "")
        _log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="edited_text",
            old_value=old_text,
            new_value=data.edited_text
        )

    if data.is_reviewed is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="is_reviewed",
            old_value=old_chunk.get("is_reviewed", False),
            new_value=data.is_reviewed
        )

    if data.is_excluded is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="is_excluded",
            old_value=old_chunk.get("is_excluded", False),
            new_value=data.is_excluded
        )

        # If excluding a chunk and document is completed (already ingested), remove from Marqo
        if data.is_excluded and not old_chunk.get("is_excluded", False):
            if doc and doc.get("stage") == "completed":
                doc_id = doc.get("document_id")
                if doc_id:
                    marqo_result = delete_single_chunk_from_marqo(doc_id, chunk_num)
                    if marqo_result.get("deleted"):
                        _log_audit(
                            workflow_id=workflow_id,
                            action_type="chunk_removed_from_search",
                            entity_type="chunk",
                            entity_id=chunk_num,
                            metadata={"marqo_id": marqo_result.get("chunk_id")}
                        )

    if data.reviewer_notes is not None:
        _log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="reviewer_notes",
            old_value=old_chunk.get("reviewer_notes"),
            new_value=data.reviewer_notes
        )

    tags_changed = False
    if data.domain_tags is not None:
        from .domain_tags.base import parse_tag_list, validate_tags_against_taxonomy
        from .domain_tags.service import load_domain_tagging_config

        config = load_domain_tagging_config()
        parsed = parse_tag_list(data.domain_tags, source="manual")
        if config.strict_taxonomy:
            parsed = validate_tags_against_taxonomy(parsed, strict=True)
        db.replace_chunk_tags(
            workflow_id,
            chunk_num,
            [{"dimension": t.dimension, "value": t.value} for t in parsed],
            source="manual",
        )
        tags_changed = True
        _log_audit(
            workflow_id=workflow_id,
            action_type="chunk_tag_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="domain_tags",
            old_value=old_chunk.get("domain_tags_flat"),
            new_value="|".join(sorted(t.key() for t in parsed)),
        )

    if data.edited_text is not None or data.is_excluded is not None or tags_changed:
        reason = "Chunk tags changed; search index is out of sync" if tags_changed and data.edited_text is None and data.is_excluded is None else "Chunk content changed; search index is out of sync"
        _mark_reindex_required(
            workflow_id,
            reason,
            metadata={"chunk_number": chunk_num},
        )

    return db.get_chunk(workflow_id, chunk_num)


@app.put("/documents/{workflow_id}/chunks/{chunk_num}/tags")
async def set_chunk_tags(
    workflow_id: str,
    data: ChunkTagsUpdate,
    user: RequireReview,
    chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)"),
):
    """Replace manual domain tags on a chunk (dimension:value strings)."""
    _require_document_for_user(workflow_id, user)
    old_chunk = db.get_chunk(workflow_id, chunk_num)
    if not old_chunk:
        raise HTTPException(404, f"Chunk {chunk_num} not found")

    from .domain_tags.base import parse_tag_list, validate_tags_against_taxonomy
    from .domain_tags.service import load_domain_tagging_config

    config = load_domain_tagging_config()
    parsed = parse_tag_list(data.tags, source="manual")
    if config.strict_taxonomy:
        parsed = validate_tags_against_taxonomy(parsed, strict=True)
    db.replace_chunk_tags(
        workflow_id,
        chunk_num,
        [{"dimension": t.dimension, "value": t.value} for t in parsed],
        source="manual",
    )
    _log_audit(
        workflow_id=workflow_id,
        action_type="chunk_tag_edit",
        entity_type="chunk",
        entity_id=chunk_num,
        field_name="domain_tags",
        old_value=old_chunk.get("domain_tags_flat"),
        new_value="|".join(sorted(t.key() for t in parsed)),
    )
    _mark_reindex_required(
        workflow_id,
        "Chunk tags changed; search index is out of sync",
        metadata={"chunk_number": chunk_num},
    )
    return db.get_chunk(workflow_id, chunk_num)


@app.post("/documents/{workflow_id}/auto-tag-chunks")
async def auto_tag_document_chunks(workflow_id: str, user: RequireReview):
    """Re-run automatic domain tagging for all chunks in a document."""
    doc = _require_document_for_user(workflow_id, user)

    from .domain_tags.gemma_tagger import auto_tag_chunks
    from .domain_tags.service import get_domain_tagger, load_domain_tagging_config

    config = load_domain_tagging_config()
    if not config.enabled:
        raise HTTPException(400, "Domain tagging is disabled (DOMAIN_TAGGING_ENABLED=false)")

    chunks = db.get_chunks(workflow_id, include_excluded=True)
    if not chunks:
        raise HTTPException(400, "No chunks available for tagging")

    doc_context = " | ".join(
        part for part in [doc.get("source_manifest_name"), doc.get("display_name")] if part
    )
    tagger = get_domain_tagger(config)
    tagged_map = await auto_tag_chunks(
        chunks,
        filename=doc.get("filename") or "",
        doc_context=doc_context,
        tagger=tagger,
    )
    db.delete_auto_chunk_tags(workflow_id)
    tagged_chunks = 0
    total_tags = 0
    for chunk_num, tags in tagged_map.items():
        if not tags:
            continue
        db.replace_chunk_tags(
            workflow_id,
            chunk_num,
            [{"dimension": t.dimension, "value": t.value} for t in tags],
            source="auto",
        )
        tagged_chunks += 1
        total_tags += len(tags)

    if tagged_chunks:
        _mark_reindex_required(
            workflow_id,
            "Auto domain tags updated; search index is out of sync",
            metadata={"tagged_chunks": tagged_chunks},
        )

    return {
        "workflow_id": workflow_id,
        "tagged_chunks": tagged_chunks,
        "total_tags": total_tags,
    }


@app.get("/taxonomy/domain-tags")
async def get_domain_tag_taxonomy():
    """Return the domain tag taxonomy for UI editors."""
    from .domain_tags.service import get_taxonomy_for_api

    return get_taxonomy_for_api()


@app.post("/documents/{workflow_id}/chunks/{chunk_num}/reset")
async def reset_chunk(
    workflow_id: str,
    user: RequireReview,
    chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)"),
):
    """Reset chunk to original text."""
    _require_document_for_user(workflow_id, user)
    old_chunk = db.get_chunk(workflow_id, chunk_num)
    if not old_chunk:
        raise HTTPException(404, f"Chunk {chunk_num} not found")
    db.reset_chunk(workflow_id, chunk_num)

    # Log reset action
    _log_audit(
        workflow_id=workflow_id,
        action_type="chunk_reset",
        entity_type="chunk",
        entity_id=chunk_num,
        field_name="edited_text",
        old_value=old_chunk.get("edited_text") if old_chunk else None,
        new_value=None,
        metadata={"reset_to": "original_text"}
    )
    _mark_reindex_required(
        workflow_id,
        "Chunk reset; search index is out of sync",
        metadata={"chunk_number": chunk_num},
    )

    return db.get_chunk(workflow_id, chunk_num)


# =============================================================================
# Export Routes
# =============================================================================

@app.get("/documents/{workflow_id}/export/markdown")
async def export_markdown(workflow_id: str):
    """Export document as combined markdown."""
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")

    pages = db.get_pages(workflow_id)
    content = []
    for page in pages:
        md = (
            page.get("edited_translation")
            or page.get("translated_markdown")
            or page.get("edited_markdown")
            or page.get("original_markdown", "")
        )
        content.append(f"<!-- Page {page.get('page_number')} -->\n\n{md}")

    return {
        "filename": doc.get("filename", "").replace(".pdf", ".md"),
        "content": "\n\n---\n\n".join(content)
    }


@app.get("/documents/{workflow_id}/export/chunks")
async def export_chunks(workflow_id: str, include_excluded: bool = False):
    """Export chunks as JSON for Marqo ingestion."""
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Workflow not found: {workflow_id}")

    chunks = db.get_chunks(workflow_id, include_excluded=include_excluded)
    doc_id = doc.get("document_id", "")
    filename = doc.get("filename", "")
    name = filename.replace(".pdf", "")

    records = []
    for chunk in chunks:
        text = chunk.get("edited_text") or chunk.get("original_text", "")
        chunk_num = chunk.get("chunk_number", 0)

        records.append({
            "_id": hashlib.md5(f"{doc_id}_{chunk_num}_{text[:50]}".encode()).hexdigest(),
            "doc_id": doc_id,
            "name": name,
            "text": text,
            "chunk_num": chunk_num,
            "token_count": chunk.get("token_count", 0),
            "source": "docs-pipeline"
        })

    return records


# =============================================================================
# PDF Serving
# =============================================================================

def _inline_content_disposition(filename: str) -> str:
    """Build a latin-1-safe Content-Disposition header for inline file display."""
    # Strip CR/LF/NUL so a crafted filename cannot inject response headers.
    safe_name = (filename or "document.pdf").replace('"', "'")
    safe_name = "".join(ch for ch in safe_name if ch not in "\r\n\0").strip() or "document.pdf"
    try:
        safe_name.encode("latin-1")
        return f'inline; filename="{safe_name}"'
    except UnicodeEncodeError:
        ascii_name = safe_name.encode("ascii", "ignore").decode("ascii").strip() or "document.pdf"
        encoded_name = quote(safe_name)
        return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"


@app.get("/documents/{workflow_id}/pdf")
async def get_document_pdf(workflow_id: str):
    """
    Get the original PDF file for a document.
    Returns the PDF as a streaming response. SQLite-first for speed.
    """
    # SQLite-first - instant lookup
    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")

    filepath = doc.get("filepath", "")
    filename = doc.get("filename", "document.pdf")

    if not filepath:
        raise HTTPException(404, f"Document has no PDF path: {workflow_id}")

    try:
        if filepath.startswith("minio://"):
            # Parse minio://bucket/object/path
            path = filepath.replace("minio://", "")
            parts = path.split("/", 1)
            bucket = parts[0]
            object_name = parts[1] if len(parts) > 1 else ""

            # Get object from MinIO
            response = minio_client.get_object(bucket, object_name)

            return StreamingResponse(
                response,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": _inline_content_disposition(filename)
                }
            )
        else:
            # Local file
            file_path = Path(filepath)
            if not file_path.exists():
                raise HTTPException(404, f"PDF file not found: {filepath}")

            def file_iterator():
                with open(file_path, "rb") as f:
                    yield from f

            return StreamingResponse(
                file_iterator(),
                media_type="application/pdf",
                headers={
                    "Content-Disposition": _inline_content_disposition(filename)
                }
            )
    except HTTPException:
        raise
    except Exception as e:
        # Log the actual error server-side but don't expose details to client
        logging.error(f"PDF serving error for {workflow_id}: {str(e)}")
        raise HTTPException(500, "Error serving PDF file")


@app.get("/provenance/chunk")
async def resolve_provenance_chunk(
    request: Request,
    doc_id: Optional[str] = Query(None, description="workflow slug, SQLite document_id, or legacy Marqo doc_id"),
    chunk_num: Optional[int] = Query(None, alias="chunk_num"),
    marqo_id: Optional[str] = Query(None, description="Marqo _id for a single indexed chunk"),
    index_name: str = Query("documents-index"),
):
    """
    Resolve a retrieved chunk to workflow metadata and maintainer URLs.

    Used by chat/retrieval clients when Marqo hits lack workflow_id (legacy rows) or for enrichment.
    """
    from .activities import _infer_section

    resolved_doc_id = doc_id
    resolved_chunk_num = chunk_num

    if marqo_id and (resolved_doc_id is None or resolved_chunk_num is None):
        import marqo

        marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
        mq = marqo.Client(url=marqo_url)
        try:
            hit = mq.index(index_name).get_document(marqo_id)
        except Exception as error:
            raise HTTPException(404, f"Marqo document not found: {error}") from error

        resolved_doc_id = (
            hit.get("workflow_id")
            or hit.get("doc_id")
            or hit.get("filename")
        )
        resolved_chunk_num = hit.get("chunk_num")
        if resolved_chunk_num is None:
            resolved_chunk_num = hit.get("chunk_index")
        if not resolved_doc_id or resolved_chunk_num is None:
            raise HTTPException(404, "Marqo document is missing doc_id/workflow_id or chunk_num")

    if not resolved_doc_id or resolved_chunk_num is None:
        raise HTTPException(400, "Provide doc_id and chunk_num, or marqo_id")

    provenance = db.resolve_chunk_provenance(doc_id=resolved_doc_id, chunk_num=int(resolved_chunk_num))
    if not provenance:
        raise HTTPException(404, "Chunk provenance not found")

    workflow_id = provenance["workflow_id"]
    chunk = db.get_chunk(workflow_id, int(resolved_chunk_num))
    if chunk:
        text = chunk.get("edited_text") or chunk.get("original_text") or ""
        provenance["section"] = _infer_section(text, chunk.get("section_title"))
        provenance["excerpt"] = text[:320] + ("..." if len(text) > 320 else "")

    links = _build_provenance_links(workflow_id, int(resolved_chunk_num), request)
    return {**provenance, **links}


@app.get("/documents/{workflow_id}/marqo")
async def get_document_marqo_status(
    workflow_id: str,
    index_name: str = Query("documents-index"),
):
    import marqo

    doc = db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")

    marqo_doc_id = get_marqo_doc_id(doc["document_id"])
    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    mq = marqo.Client(url=marqo_url)
    index = mq.index(index_name)
    result = index.search(
        q="",
        filter_string=f"doc_id:{marqo_doc_id}",
        limit=1000,
        attributes_to_retrieve=[
            "doc_id",
            "filename",
            "text",
            "chunk_num",
            "page_start",
            "page_end",
            "token_count",
            "is_reference",
        ],
    )
    raw_hits = result.get("hits", [])
    hits = []
    for hit in raw_hits:
        normalized_hit = dict(hit)
        chunk_num = normalized_hit.get("chunk_num")
        normalized_hit.setdefault(
            "_id",
            f"{normalized_hit.get('doc_id', marqo_doc_id)}:{chunk_num if chunk_num is not None else 'unknown'}",
        )
        normalized_hit.setdefault("chunk_number", chunk_num)
        hits.append(normalized_hit)
    sqlite_chunks = db.get_chunks(workflow_id, include_excluded=True)
    status = {
        "workflow_id": workflow_id,
        "index_name": index_name,
        "marqo_doc_id": marqo_doc_id,
        "sqlite_chunk_count": len([c for c in sqlite_chunks if not c.get("is_excluded")]),
        "indexed_chunk_count": len(hits),
        "status": "indexed" if hits else "missing",
        "hits": hits,
    }
    db.upsert_document_index_status(
        workflow_id=workflow_id,
        index_name=index_name,
        marqo_doc_id=marqo_doc_id,
        chunk_count_indexed=len(hits),
        last_verified_at=datetime.utcnow().isoformat(),
        status=status["status"],
        details={"sqlite_chunk_count": status["sqlite_chunk_count"]},
    )
    return status


@app.get("/documents/{workflow_id}/marqo/chunks")
async def list_document_marqo_chunks(
    workflow_id: str,
    index_name: str = Query("documents-index"),
):
    result = await get_document_marqo_status(workflow_id, index_name=index_name)
    return result["hits"]


@app.get("/marqo/indexes/{index_name}/settings")
async def get_marqo_index_settings(index_name: str):
    import marqo

    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    mq = marqo.Client(url=marqo_url)
    return mq.index(index_name).get_settings()


@app.get("/marqo/indexes/{index_name}/stats")
async def get_marqo_index_stats(index_name: str):
    import marqo

    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    mq = marqo.Client(url=marqo_url)
    return mq.index(index_name).get_stats()


@app.get("/marqo/indexes/summary")
async def get_marqo_indexes_summary(
    x_include_demo: Optional[str] = Header(None, alias="X-Include-Demo"),
    x_include_disabled: Optional[str] = Header(None, alias="X-Include-Disabled"),
):
    """Summarize index coverage from SQLite-backed index status plus live Marqo stats."""
    include_demo = x_include_demo and x_include_demo.lower() == "true"
    include_disabled = x_include_disabled and x_include_disabled.lower() == "true"
    summaries = db.list_index_summaries(
        include_demo=include_demo,
        include_disabled=include_disabled,
    )
    if not summaries:
        return []

    import marqo
    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    mq = marqo.Client(url=marqo_url)

    results = []
    for summary in summaries:
        live_stats = None
        live_error = None
        has_domain_tags_field = None
        try:
            live_stats = mq.index(summary["index_name"]).get_stats()
        except Exception as exc:
            live_error = str(exc)
        try:
            index_settings = mq.index(summary["index_name"]).get_settings()
            field_names = {
                f.get("name")
                for f in (index_settings.get("allFields") or [])
                if isinstance(f, dict) and f.get("name")
            }
            has_domain_tags_field = "domain_tags" in field_names
        except Exception:
            has_domain_tags_field = None
        results.append({
            **summary,
            "live_stats": live_stats,
            "live_error": live_error,
            "has_domain_tags_field": has_domain_tags_field,
        })
    return results


@app.post("/marqo/search")
async def run_marqo_search(payload: dict):
    import marqo

    settings = db.get_search_settings()
    index_name = payload.get("index_name") or settings.get("indexName") or "documents-index"
    query = (payload.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query is required")

    search_mode = (payload.get("search_mode") or settings.get("searchMethod") or "HYBRID").upper()
    top_k = max(1, min(int(payload.get("top_k") or settings.get("limit") or 12), 50))
    candidate_multiplier = max(1, int(payload.get("candidate_multiplier") or settings.get("candidateMultiplier") or 10))
    requested_candidate_cap = payload.get("candidate_cap")
    if requested_candidate_cap is None:
        candidate_cap = min(max(top_k * candidate_multiplier, top_k), int(settings.get("candidateCap") or 120))
    else:
        candidate_cap = int(requested_candidate_cap)
    candidate_cap = max(top_k, min(candidate_cap, 200))
    max_chunks_per_doc = max(1, int(payload.get("max_chunks_per_doc") or settings.get("maxChunksPerDoc") or 2))
    use_e5_prefix = bool(payload.get("use_e5_prefix", settings.get("useE5Prefix", True)))
    exclude_reference = bool(payload.get("exclude_reference", settings.get("excludeReference", True)))
    alpha = float(payload.get("hybrid_alpha") or settings.get("alpha") or 0.6)
    ranking_method = payload.get("ranking_method") or settings.get("rankingMethod") or "rrf"
    ef_search = int(payload.get("ef_search") or settings.get("efSearch") or 256)
    query_expansion_profile = payload.get("query_expansion_profile") or settings.get("queryExpansionProfile") or "gu-v1"
    rerank_mode = payload.get("rerank_mode") or settings.get("rerankMode") or "none"
    hybrid_rrf_k = int(payload.get("hybrid_rrf_k") or settings.get("hybridRrfK") or 60)
    domain_tag_filters = payload.get("domain_tags") or payload.get("domain_tag_filters") or []
    if isinstance(domain_tag_filters, str):
        domain_tag_filters = [domain_tag_filters]
    expanded_query = _expand_query(query, query_expansion_profile)
    effective_query = _prepare_query_for_e5(expanded_query) if use_e5_prefix else expanded_query

    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    mq = marqo.Client(url=marqo_url)
    index = mq.index(index_name)

    request = {
        "q": effective_query,
        "limit": candidate_cap,
        "search_method": search_mode.lower(),
        "ef_search": ef_search,
    }
    if exclude_reference:
        request["filter_string"] = "is_reference:false"
    if search_mode == "HYBRID":
        request["hybrid_parameters"] = {
            "alpha": alpha,
            "rankingMethod": ranking_method,
            "rrfK": hybrid_rrf_k,
            "searchableAttributesLexical": ["text", "description"],
            "searchableAttributesTensor": ["text_for_embedding"],
        }
    elif search_mode == "TENSOR":
        request["searchable_attributes"] = ["text_for_embedding"]
    else:
        request["searchable_attributes"] = ["text", "description"]

    from .domain_tags.base import build_marqo_domain_tags_filter, merge_marqo_filter_strings

    reference_filter = "is_reference:false" if exclude_reference else None
    tag_filter = build_marqo_domain_tags_filter(domain_tag_filters)
    if tag_filter:
        try:
            index_settings = index.get_settings()
            field_names = {
                field.get("name")
                for field in (index_settings.get("allFields") or [])
                if isinstance(field, dict) and field.get("name")
            }
        except Exception as error:
            raise HTTPException(400, f"Unable to inspect index schema for '{index_name}': {error}") from error
        if "domain_tags" not in field_names:
            raise HTTPException(
                400,
                (
                    f"Index '{index_name}' does not support domain tag filters yet. "
                    "Use an index created with the passage schema that includes 'domain_tags' "
                    "(for example: documents-index-tags)."
                ),
            )
    filter_string = merge_marqo_filter_strings(reference_filter, tag_filter)
    if filter_string:
        request["filter_string"] = filter_string

    try:
        result = index.search(**request)
    except MarqoError as error:
        raise HTTPException(400, f"Marqo search failed: {error}") from error
    except Exception as error:
        raise HTTPException(400, f"Marqo search failed: {error}") from error
    hits = result.get("hits", [])
    hits = _rerank_hits(query, hits, rerank_mode)
    final_hits = []
    per_doc_counts: dict[str, int] = {}
    for hit in hits:
        doc_key = hit.get("doc_id") or hit.get("filename") or "__unknown__"
        if per_doc_counts.get(doc_key, 0) >= max_chunks_per_doc:
            continue
        per_doc_counts[doc_key] = per_doc_counts.get(doc_key, 0) + 1
        final_hits.append(hit)
        if len(final_hits) >= top_k:
            break

    for hit in final_hits:
        if hit.get("domain_tags"):
            continue
        doc_id = hit.get("doc_id")
        chunk_num = hit.get("chunk_num") if hit.get("chunk_num") is not None else hit.get("chunk_number")
        if not doc_id or chunk_num is None:
            continue
        flat_tags = db.get_domain_tags_flat_for_document_chunk(str(doc_id), int(chunk_num))
        if flat_tags:
            hit["domain_tags"] = flat_tags
            hit["domain_tags_source"] = "sqlite"

    return {
        "effective_config": {
            "index_name": index_name,
            "query": query,
            "search_mode": search_mode,
            "top_k": top_k,
            "candidate_cap": candidate_cap,
            "candidate_multiplier": candidate_multiplier,
            "max_chunks_per_doc": max_chunks_per_doc,
            "use_e5_prefix": use_e5_prefix,
            "exclude_reference": exclude_reference,
            "hybrid_alpha": alpha,
            "ranking_method": ranking_method,
            "hybrid_rrf_k": hybrid_rrf_k,
            "ef_search": ef_search,
            "query_expansion_profile": query_expansion_profile,
            "query_expansion_applied": expanded_query != query,
            "rerank_mode": rerank_mode,
            "domain_tags": list(domain_tag_filters) if domain_tag_filters else [],
            "filter_string": filter_string,
        },
        "candidate_count": len(hits),
        "final_count": len(final_hits),
        "hits": final_hits,
        "raw_hits": hits if payload.get("include_raw_hits") else None,
    }


# =============================================================================
# Health
# =============================================================================

@app.get("/health")
async def health():
    """Health check."""
    return {
        "status": "ok",
        "temporal_connected": temporal_client is not None
    }


# =============================================================================
# Marqo index (passage schema)
# =============================================================================

@app.get("/admin/index/schema")
async def get_marqo_index_schema(
    index_name: str = Query("documents-index", description="Marqo index name"),
):
    """Report whether the live Marqo index includes filterable domain_tags."""
    import marqo
    from .activities import _passage_schema_field_names

    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    mq = marqo.Client(url=marqo_url)
    try:
        index = mq.index(index_name)
        index_settings = index.get_settings()
    except Exception as exc:
        raise HTTPException(404, f"Index '{index_name}' not found: {exc}") from exc

    field_names = sorted(
        f.get("name")
        for f in (index_settings.get("allFields") or [])
        if isinstance(f, dict) and f.get("name")
    )
    has_domain_tags_field = "domain_tags" in set(field_names)
    canonical_fields = sorted(_passage_schema_field_names())
    missing_fields = sorted(set(canonical_fields) - set(field_names))

    return {
        "index_name": index_name,
        "marqo_url": marqo_url,
        "has_domain_tags_field": has_domain_tags_field,
        "fields": field_names,
        "canonical_passage_fields": canonical_fields,
        "missing_canonical_fields": missing_fields,
        "domain_tags_ready": has_domain_tags_field,
        "note": (
            "Structured Marqo indexes cannot add fields after creation. "
            "If domain_tags is missing, recreate the index with the passage schema "
            "and reingest documents to enable tag filtering in search."
        ),
    }


@app.post("/admin/index/create")
async def create_marqo_index(
    user: RequireAdmin,
    index_name: str = Query("documents-index", description="Marqo index name"),
    recreate_if_exists: bool = Query(False, description="If true, delete existing index and create with passage schema"),
):
    """
    Create the Marqo index with the passage schema (E5 text_for_embedding + full metadata).

    Use this to ensure the index exists with the correct schema before reingest, or to
    reset the index to the canonical schema. Marqo URL from MARQO_URL env (default http://localhost:8882).
    """
    _ = user
    import marqo
    from .activities import _marqo_settings

    marqo_url = os.environ.get("MARQO_URL", "http://localhost:8882")
    mq = marqo.Client(url=marqo_url)
    settings = _marqo_settings(use_tensor_prefix_field=True)

    try:
        mq.get_index(index_name)
        index_exists = True
    except Exception:
        index_exists = False

    if index_exists and not recreate_if_exists:
        return {
            "index": index_name,
            "created": False,
            "message": "Index already exists. Use recreate_if_exists=true to replace with passage schema.",
        }

    if index_exists and recreate_if_exists:
        mq.delete_index(index_name)

    mq.create_index(index_name, settings_dict=settings)
    return {
        "index": index_name,
        "created": True,
        "message": "Index created with passage schema (text_for_embedding, full metadata).",
        "marqo_url": marqo_url,
    }


@app.get("/admin/ingest-info")
async def get_ingest_info():
    """
    Return what the running container's ingest code would send to Marqo.
    Use this to verify the API/worker image has the passage schema (text_for_embedding, etc.).
    """
    from .activities import _passage_schema_field_names, _prepare_records

    passage_fields = sorted(_passage_schema_field_names())
    has_text_for_embedding = "text_for_embedding" in set(passage_fields)
    # One fake chunk to see exact record shape the worker would send
    fake_chunk = {
        "chunk_number": 0,
        "original_text": "Sample text.",
        "edited_text": None,
        "is_excluded": False,
        "token_count": 2,
        "page_start": 1,
        "page_end": 1,
    }
    sample_records = _prepare_records(
        document_id="debug-document-id",
        filename="debug.pdf",
        chunks=[fake_chunk],
        workflow_id="doc-debugsample12",
    )
    sample_record_keys = sorted(sample_records[0].keys()) if sample_records else []
    return {
        "passage_schema_fields": passage_fields,
        "has_text_for_embedding": has_text_for_embedding,
        "sample_record_keys": sample_record_keys,
        "sample_has_passage_prefix": (
            sample_records[0].get("text_for_embedding", "").startswith("passage:")
            if sample_records else False
        ),
    }


@app.post("/documents/reconcile")
async def reconcile_document_states(user: RequirePipeline):
    """
    Reconcile SQLite document states with Temporal workflow states.

    This endpoint checks all documents in processing/review stages and updates
    SQLite if the Temporal workflow has terminated or failed. This fixes
    inconsistencies caused by external workflow termination or worker crashes.

    Returns a summary of documents checked and updated.
    """
    # Stages that indicate an active workflow (not terminal states)
    active_stages = [
        'ocr_processing', 'ocr_review',
        'translation_processing', 'translation_review',
        'chunking', 'chunk_review',
        'ready_for_ingestion', 'ingesting'
    ]

    # Scope to caller's instances (None = unrestricted bypass / all tenants).
    docs = db.list_documents(
        limit=1000,
        include_demo=True,
        include_disabled=True,
        instances=_instance_scope_for_user(user),
    )
    active_docs = [d for d in docs if d.get('stage') in active_stages]

    results = {
        "checked": len(active_docs),
        "updated": 0,
        "still_running": 0,
        "details": []
    }

    for doc in active_docs:
        detail = await _reconcile_single_document(doc)
        results["details"].append(detail)
        if detail.get("action") == "stage_synced" or detail.get("action") == "marked_failed":
            results["updated"] += 1
        elif detail.get("action") == "no_change":
            results["still_running"] += 1

    return results


@app.get("/pipeline/stages")
async def get_pipeline_stages():
    """Get the pipeline stages for UI stepper display."""
    return [
        {"id": stage[0], "label": stage[1], "description": stage[2]}
        for stage in PIPELINE_STAGES
    ]


# =============================================================================
# Settings Routes
# =============================================================================

@app.get("/settings/search", response_model=SearchSettings)
async def get_search_settings():
    """
    Get current search settings.

    Returns the current search configuration including:
    - searchMethod: TENSOR, LEXICAL, or HYBRID
    - limit: Number of results to return
    - alpha: Balance between lexical (0) and semantic (1) for hybrid search
    - rankingMethod: rrf or normalize_linear for hybrid search
    - showHighlights: Whether to show highlighted matches
    - efSearch: HNSW search accuracy parameter
    """
    return db.get_search_settings()


@app.put("/settings/search", response_model=SearchSettings)
async def update_search_settings_endpoint(settings: SearchSettingsUpdate, user: RequireAdmin):
    """
    Update search settings.

    Only provided fields will be updated. Changes are logged to the audit trail.
    """
    _ = user
    # Convert to dict, excluding None values
    updates = {k: v for k, v in settings.model_dump().items() if v is not None}

    if not updates:
        raise HTTPException(400, "No settings provided to update")

    return db.update_search_settings(updates)


@app.get("/settings/search/audit", response_model=SettingsAuditResponse)
async def get_search_settings_audit(
    limit: int = Query(50, le=200),
    offset: int = 0
):
    """
    Get audit trail for search settings changes.

    Shows all historical changes to search settings with old/new values.
    """
    logs = db.get_settings_audit_logs(limit=limit, offset=offset)
    total = db.get_settings_audit_count()

    return SettingsAuditResponse(
        logs=logs,
        total=total,
        limit=limit,
        offset=offset
    )


@app.post("/settings/search/reset", response_model=SearchSettings)
async def reset_search_settings(user: RequireAdmin):
    """
    Reset search settings to defaults.

    Resets all search settings to their default values:
    - searchMethod: HYBRID
    - limit: 10
    - alpha: 0.7
    - rankingMethod: rrf
    - showHighlights: true
    - efSearch: 256
    """
    _ = user
    defaults = {
        "searchMethod": "HYBRID",
        "limit": 12,
        "alpha": 0.6,
        "rankingMethod": "rrf",
        "showHighlights": True,
        "efSearch": 256,
        "indexName": "documents-index",
        "candidateCap": 120,
        "candidateMultiplier": 10,
        "maxChunksPerDoc": 2,
        "useE5Prefix": True,
        "excludeReference": True,
        "queryExpansionProfile": "gu-v1",
        "rerankMode": "none",
        "hybridRrfK": 60,
    }
    return db.update_search_settings(defaults)
