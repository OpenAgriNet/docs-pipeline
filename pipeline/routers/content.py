"""Pages, chunks, tags, exports and provenance."""

import hashlib
from datetime import datetime
from fastapi import APIRouter, HTTPException, Path as PathParam, Query, Request
from typing import Optional
from ..auth.deps import RequireAdmin, RequireReview, RequireSearch
from ..auth.permissions import Permission
from ..models import ChunkTagsUpdate, ChunkUpdate, DocumentStage, PageUpdate
from ..vector_store import (
    VectorStoreError,
    get_marqo_doc_id,
    merge_filter_strings,
    term_filter,
)

router = APIRouter()


@router.get("/documents/{workflow_id}/pages")
async def list_pages(workflow_id: str, user: RequireSearch):
    """Get all pages for a document. SQLite-first for speed."""
    # Enforce tenant scope up front (404 hides other tenants' documents).
    api._require_document_for_user(workflow_id, user)
    return api.db.get_pages(workflow_id)


@router.get("/documents/{workflow_id}/pages/{page_num}")
async def get_page(workflow_id: str, user: RequireSearch, page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)")):
    """Get a specific page. SQLite-first for speed."""
    api._require_document_for_user(workflow_id, user)
    page = api.db.get_page(workflow_id, page_num)
    if page:
        return page

    raise HTTPException(404, f"Page {page_num} not found")


@router.patch("/documents/{workflow_id}/pages/{page_num}")
async def update_page(
    workflow_id: str,
    data: PageUpdate,
    user: RequireReview,
    page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)"),
):
    """Update a page (edit markdown, mark reviewed)."""
    doc = api._require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    old_page = api.db.get_page(workflow_id, page_num)
    if not old_page:
        raise HTTPException(404, f"Page {page_num} not found")

    updated = api.db.update_page(
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
        api._log_audit(
            workflow_id=workflow_id,
            action_type="page_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="edited_markdown",
            old_value=old_text,
            new_value=data.edited_markdown
        )

    if data.is_reviewed is not None:
        api._log_audit(
            workflow_id=workflow_id,
            action_type="page_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="is_reviewed",
            old_value=old_page.get("is_reviewed", False),
            new_value=data.is_reviewed
        )

    if data.reviewer_notes is not None:
        api._log_audit(
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
        api._log_audit(
            workflow_id=workflow_id,
            action_type="translation_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="edited_translation",
            old_value=old_translation,
            new_value=data.edited_translation
        )

    if data.translation_reviewed is not None:
        api._log_audit(
            workflow_id=workflow_id,
            action_type="translation_edit",
            entity_type="page",
            entity_id=page_num,
            field_name="translation_reviewed",
            old_value=old_page.get("translation_reviewed", False),
            new_value=data.translation_reviewed
        )

    if data.translation_notes is not None:
        api._log_audit(
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
        api._mark_reindex_required(
            workflow_id,
            "Page content changed after chunk generation; rechunk and reindex required",
            metadata={"page_number": page_num},
        )

    return api.db.get_page(workflow_id, page_num)


@router.post("/documents/{workflow_id}/pages/{page_num}/reset")
async def reset_page(
    workflow_id: str,
    user: RequireReview,
    page_num: int = PathParam(..., ge=1, le=10000, description="Page number (1-indexed)"),
):
    """Reset page to original OCR output."""
    doc = api._require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    old_page = api.db.get_page(workflow_id, page_num)
    if not old_page:
        raise HTTPException(404, f"Page {page_num} not found")
    api.db.reset_page(workflow_id, page_num)

    # Log reset action
    api._log_audit(
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
        api._mark_reindex_required(
            workflow_id,
            "Page reset after chunk generation; rechunk and reindex required",
            metadata={"page_number": page_num},
        )

    return api.db.get_page(workflow_id, page_num)


@router.get("/chunks/search")
async def search_chunks_across_documents(
    user: RequireSearch,
    q: str = Query("", description="Keyword search within chunk text"),
    tags: Optional[list[str]] = Query(None, description="Repeatable dimension:value filter"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    include_excluded: bool = Query(False, description="Include excluded chunks"),
    stage: Optional[DocumentStage] = Query(None, description="Optional document stage filter"),
):
    """Search chunks across all documents for KB maintainer workflows."""
    # Tenant-scope via the owning document's instance (None = data-unrestricted
    # bypass only; a control-plane master_admin scopes to its empty tenant set).
    chunks, total = api.db.search_chunks(
        query=q,
        tags=tags or [],
        limit=limit,
        offset=offset,
        include_excluded=include_excluded,
        stage=stage.value if stage else None,
        instances=api._instance_scope_for_user(user),
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


@router.get("/documents/{workflow_id}/chunks")
async def list_chunks(workflow_id: str, user: RequireSearch, include_excluded: bool = False):
    """Get all chunks for a document. SQLite-first for speed."""
    api._require_document_for_user(workflow_id, user)
    return api.db.get_chunks(workflow_id, include_excluded=include_excluded)


@router.get("/documents/{workflow_id}/chunks/{chunk_num}")
async def get_chunk(workflow_id: str, user: RequireSearch, chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)")):
    """Get a specific chunk. SQLite-first for speed."""
    api._require_document_for_user(workflow_id, user)
    chunk = api.db.get_chunk(workflow_id, chunk_num)
    if chunk:
        return chunk

    raise HTTPException(404, f"Chunk {chunk_num} not found")


@router.patch("/documents/{workflow_id}/chunks/{chunk_num}")
async def update_chunk(
    workflow_id: str,
    data: ChunkUpdate,
    user: RequireReview,
    chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)"),
):
    """Update a chunk (edit text, mark reviewed, exclude)."""
    doc = api._require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    old_chunk = api.db.get_chunk(workflow_id, chunk_num)
    if not old_chunk:
        raise HTTPException(404, f"Chunk {chunk_num} not found")

    updated = api.db.update_chunk(
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
        api._log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="edited_text",
            old_value=old_text,
            new_value=data.edited_text
        )

    if data.is_reviewed is not None:
        api._log_audit(
            workflow_id=workflow_id,
            action_type="chunk_edit",
            entity_type="chunk",
            entity_id=chunk_num,
            field_name="is_reviewed",
            old_value=old_chunk.get("is_reviewed", False),
            new_value=data.is_reviewed
        )

    if data.is_excluded is not None:
        api._log_audit(
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
                    target_index = api.resolve_index(doc.get("instance"), doc.get("index"))
                    if target_index is not None:
                        marqo_result = api.delete_single_chunk_from_marqo(
                            doc_id, chunk_num, index_name=target_index,
                            workflow_id=workflow_id,
                        )
                        if marqo_result.get("deleted"):
                            api._log_audit(
                                workflow_id=workflow_id,
                                action_type="chunk_removed_from_search",
                                entity_type="chunk",
                                entity_id=chunk_num,
                                metadata={"marqo_id": marqo_result.get("chunk_id")}
                            )

    if data.reviewer_notes is not None:
        api._log_audit(
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
        from ..domain_tags.base import (
            load_taxonomy_for_instance,
            parse_tag_list,
            validate_tags_against_taxonomy,
        )
        from ..domain_tags.service import load_domain_tagging_config

        config = load_domain_tagging_config()
        parsed = parse_tag_list(data.domain_tags, source="manual")
        if config.strict_taxonomy:
            taxonomy = load_taxonomy_for_instance(doc.get("instance"))
            parsed = validate_tags_against_taxonomy(parsed, taxonomy, strict=True)
        api.db.replace_chunk_tags(
            workflow_id,
            chunk_num,
            [{"dimension": t.dimension, "value": t.value} for t in parsed],
            source="manual",
        )
        tags_changed = True
        api._log_audit(
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
        api._mark_reindex_required(
            workflow_id,
            reason,
            metadata={"chunk_number": chunk_num},
        )

    return api.db.get_chunk(workflow_id, chunk_num)


@router.delete("/documents/{workflow_id}/chunks/{chunk_num}")
async def delete_chunk(
    workflow_id: str,
    user: RequireAdmin,
    chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)"),
):
    """Hard-delete one chunk from SQLite and remove it from Marqo.

    Chunk numbers are not renumbered (gaps are left). Reingest does not bring
    this chunk back — only a full re-chunk of the document would recreate chunks.
    """
    doc = api._require_document_for_user(workflow_id, user, permission=Permission.ADMIN)
    if doc.get("is_disabled"):
        raise HTTPException(400, "Cannot delete chunks on a deleted document; restore it first")

    old_chunk = api.db.get_chunk(workflow_id, chunk_num)
    if not old_chunk:
        raise HTTPException(404, f"Chunk {chunk_num} not found")

    marqo_deleted = False
    marqo_chunk_id = None
    doc_id = doc.get("document_id")
    if doc_id:
        target_index = api.resolve_index(doc.get("instance"), doc.get("index"))
        if target_index is not None:
            marqo_result = api.delete_single_chunk_from_marqo(
                doc_id, chunk_num, index_name=target_index,
                workflow_id=workflow_id,
            )
            if marqo_result.get("error"):
                raise HTTPException(502, f"Failed to remove chunk from Marqo: {marqo_result['error']}")
            marqo_deleted = bool(marqo_result.get("deleted"))
            marqo_chunk_id = marqo_result.get("chunk_id")

    if not api.db.delete_chunk(workflow_id, chunk_num):
        raise HTTPException(404, f"Chunk {chunk_num} not found")

    remaining = len(api.db.get_chunks(workflow_id, include_excluded=True))
    api.db.log_audit(
        workflow_id=workflow_id,
        document_id=doc.get("document_id", ""),
        action_type="delete_chunk",
        entity_type="chunk",
        entity_id=chunk_num,
        metadata={
            "actor": user.user_id,
            "marqo_deleted": marqo_deleted,
            "marqo_chunk_id": marqo_chunk_id,
            "chunks_remaining": remaining,
            "page_start": old_chunk.get("page_start"),
            "page_end": old_chunk.get("page_end"),
        },
    )

    return {
        "workflow_id": workflow_id,
        "chunk_number": chunk_num,
        "deleted": True,
        "marqo_deleted": marqo_deleted,
        "chunks_remaining": remaining,
    }


@router.put("/documents/{workflow_id}/chunks/{chunk_num}/tags")
async def set_chunk_tags(
    workflow_id: str,
    data: ChunkTagsUpdate,
    user: RequireReview,
    chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)"),
):
    """Replace manual domain tags on a chunk (dimension:value strings)."""
    doc = api._require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    old_chunk = api.db.get_chunk(workflow_id, chunk_num)
    if not old_chunk:
        raise HTTPException(404, f"Chunk {chunk_num} not found")

    from ..domain_tags.base import (
        load_taxonomy_for_instance,
        parse_tag_list,
        validate_tags_against_taxonomy,
    )
    from ..domain_tags.service import load_domain_tagging_config

    config = load_domain_tagging_config()
    parsed = parse_tag_list(data.tags, source="manual")
    if config.strict_taxonomy:
        taxonomy = load_taxonomy_for_instance(doc.get("instance"))
        parsed = validate_tags_against_taxonomy(parsed, taxonomy, strict=True)
    api.db.replace_chunk_tags(
        workflow_id,
        chunk_num,
        [{"dimension": t.dimension, "value": t.value} for t in parsed],
        source="manual",
    )
    api._log_audit(
        workflow_id=workflow_id,
        action_type="chunk_tag_edit",
        entity_type="chunk",
        entity_id=chunk_num,
        field_name="domain_tags",
        old_value=old_chunk.get("domain_tags_flat"),
        new_value="|".join(sorted(t.key() for t in parsed)),
    )
    api._mark_reindex_required(
        workflow_id,
        "Chunk tags changed; search index is out of sync",
        metadata={"chunk_number": chunk_num},
    )
    return api.db.get_chunk(workflow_id, chunk_num)


@router.post("/documents/{workflow_id}/chunks/{chunk_num}/reset")
async def reset_chunk(
    workflow_id: str,
    user: RequireReview,
    chunk_num: int = PathParam(..., ge=1, le=10000, description="Chunk number (1-indexed)"),
):
    """Reset chunk to original text."""
    api._require_document_for_user(workflow_id, user, permission=Permission.REVIEW)
    old_chunk = api.db.get_chunk(workflow_id, chunk_num)
    if not old_chunk:
        raise HTTPException(404, f"Chunk {chunk_num} not found")
    api.db.reset_chunk(workflow_id, chunk_num)

    # Log reset action
    api._log_audit(
        workflow_id=workflow_id,
        action_type="chunk_reset",
        entity_type="chunk",
        entity_id=chunk_num,
        field_name="edited_text",
        old_value=old_chunk.get("edited_text") if old_chunk else None,
        new_value=None,
        metadata={"reset_to": "original_text"}
    )
    api._mark_reindex_required(
        workflow_id,
        "Chunk reset; search index is out of sync",
        metadata={"chunk_number": chunk_num},
    )

    return api.db.get_chunk(workflow_id, chunk_num)


@router.get("/documents/{workflow_id}/export/markdown")
async def export_markdown(workflow_id: str, user: RequireSearch):
    """Export document as combined markdown."""
    doc = api._require_document_for_user(workflow_id, user)

    pages = api.db.get_pages(workflow_id)
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


@router.get("/documents/{workflow_id}/export/chunks")
async def export_chunks(workflow_id: str, user: RequireSearch, include_excluded: bool = False):
    """Export chunks as JSON for Marqo ingestion."""
    doc = api._require_document_for_user(workflow_id, user)

    chunks = api.db.get_chunks(workflow_id, include_excluded=include_excluded)
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


@router.get("/provenance/chunk")
async def resolve_provenance_chunk(
    request: Request,
    user: RequireSearch,
    doc_id: Optional[str] = Query(None, description="workflow slug, SQLite document_id, or legacy Marqo doc_id"),
    chunk_num: Optional[int] = Query(None, alias="chunk_num"),
    marqo_id: Optional[str] = Query(None, description="Marqo _id for a single indexed chunk"),
    index_name: str = Query("documents-index"),
):
    """
    Resolve a retrieved chunk to workflow metadata and maintainer URLs.

    Used by chat/retrieval clients when Marqo hits lack workflow_id (legacy rows) or for enrichment.
    """
    from ..activities import _infer_section

    resolved_doc_id = doc_id
    resolved_chunk_num = chunk_num

    if marqo_id and (resolved_doc_id is None or resolved_chunk_num is None):
        try:
            hit = api.get_vector_store().get_document(index_name, marqo_id)
        except VectorStoreError as error:
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

    provenance = api.db.resolve_chunk_provenance(doc_id=resolved_doc_id, chunk_num=int(resolved_chunk_num))
    if not provenance:
        raise HTTPException(404, "Chunk provenance not found")

    workflow_id = provenance["workflow_id"]
    # Tenant scope: a restricted caller may not resolve another tenant's chunk.
    api._require_document_for_user(workflow_id, user)
    chunk = api.db.get_chunk(workflow_id, int(resolved_chunk_num))
    if chunk:
        text = chunk.get("edited_text") or chunk.get("original_text") or ""
        provenance["section"] = _infer_section(text, chunk.get("section_title"))
        provenance["excerpt"] = text[:320] + ("..." if len(text) > 320 else "")

    links = api._build_provenance_links(workflow_id, int(resolved_chunk_num), request)
    return {**provenance, **links}


@router.get("/documents/{workflow_id}/marqo")
async def get_document_marqo_status(
    workflow_id: str,
    user: RequireSearch,
    index_name: str = Query("documents-index"),
):
    doc = api._require_document_for_user(workflow_id, user)

    # Resolve the physical index from the doc's (instance, logical index) via the
    # registry. A caller-supplied non-default index_name is validated against its
    # owning tenant (index -> tenant -> access) before use.
    if index_name and index_name != "documents-index":
        index_name = api.assert_marqo_index_access(user, index_name)
    else:
        index_name = api.resolve_index(doc.get("instance"), doc.get("index"))

    marqo_doc_id = get_marqo_doc_id(doc["document_id"])

    # The document's tenant has no index of its own: report a graceful "no index"
    # status rather than querying (and leaking) another tenant's physical index.
    if index_name is None:
        sqlite_chunks = api.db.get_chunks(workflow_id, include_excluded=True)
        return {
            "workflow_id": workflow_id,
            "index_name": None,
            "marqo_doc_id": marqo_doc_id,
            "sqlite_chunk_count": len([c for c in sqlite_chunks if not c.get("is_excluded")]),
            "indexed_chunk_count": 0,
            "status": "no_index",
            "hits": [],
        }
    store = api.get_vector_store()
    filter_string = merge_filter_strings(
        term_filter("doc_id", marqo_doc_id),
        api._marqo_instance_filter(user, api._IndexSettingsView(store, index_name)),
    )
    result = store.search(
        index_name,
        q="",
        filter_string=filter_string,
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
    sqlite_chunks = api.db.get_chunks(workflow_id, include_excluded=True)
    status = {
        "workflow_id": workflow_id,
        "index_name": index_name,
        "marqo_doc_id": marqo_doc_id,
        "sqlite_chunk_count": len([c for c in sqlite_chunks if not c.get("is_excluded")]),
        "indexed_chunk_count": len(hits),
        "status": "indexed" if hits else "missing",
        "hits": hits,
    }
    api.db.upsert_document_index_status(
        workflow_id=workflow_id,
        index_name=index_name,
        marqo_doc_id=marqo_doc_id,
        chunk_count_indexed=len(hits),
        last_verified_at=datetime.utcnow().isoformat(),
        status=status["status"],
        details={"sqlite_chunk_count": status["sqlite_chunk_count"]},
    )
    return status


@router.get("/documents/{workflow_id}/marqo/chunks")
async def list_document_marqo_chunks(
    workflow_id: str,
    user: RequireSearch,
    index_name: str = Query("documents-index"),
):
    result = await api.get_document_marqo_status(workflow_id, user, index_name=index_name)
    return result["hits"]


# Imported last: `pipeline.api` re-exports the handlers above, so a top-level
# import here would be circular. Handlers resolve `api.<name>` at call time,
# which is what keeps `monkeypatch.setattr(api, ...)` biting.
from .. import api  # noqa: E402
