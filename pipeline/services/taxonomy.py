"""Per-tenant taxonomy lifecycle helpers."""

import logging

from fastapi import HTTPException

from .. import db

# Cap bulk auto-tag so one HTTP request cannot pin the API/LLM for hours.
BULK_AUTO_TAG_MAX_DOCS = 25
BULK_AUTO_TAG_CONCURRENCY = 2


async def auto_tag_document_chunks_impl(workflow_id: str, doc: dict) -> dict:
    """Run domain auto-tagging for every chunk in one document."""
    from ..domain_tags.base import load_taxonomy_for_instance
    from ..domain_tags.gemma_tagger import auto_tag_chunks
    from ..domain_tags.service import get_domain_tagger, load_domain_tagging_config
    from . import documents

    config = load_domain_tagging_config()
    if not config.enabled:
        raise HTTPException(400, "Domain tagging is disabled (DOMAIN_TAGGING_ENABLED=false)")

    chunks = db.get_chunks(workflow_id, include_excluded=True)
    if not chunks:
        raise HTTPException(400, "No chunks available for tagging")

    doc_context = " | ".join(
        part for part in [doc.get("source_manifest_name"), doc.get("display_name")] if part
    )
    taxonomy = load_taxonomy_for_instance(doc.get("instance"))
    tagger = get_domain_tagger(config)
    tagged_map = await auto_tag_chunks(
        chunks,
        filename=doc.get("filename") or "",
        doc_context=doc_context,
        tagger=tagger,
        taxonomy=taxonomy,
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
            [{"dimension": tag.dimension, "value": tag.value} for tag in tags],
            source="auto",
        )
        tagged_chunks += 1
        total_tags += len(tags)

    if tagged_chunks:
        documents.mark_reindex_required(
            workflow_id,
            "Auto domain tags updated; search index is out of sync",
            metadata={"tagged_chunks": tagged_chunks},
        )

    return {
        "workflow_id": workflow_id,
        "tagged_chunks": tagged_chunks,
        "total_tags": total_tags,
    }


def ensure_tenant_taxonomy_seeded(instance: str) -> None:
    """Seed a tenant taxonomy from the shipped default on first management."""
    from ..domain_tags.base import load_taxonomy

    try:
        db.seed_taxonomy_for_instance(instance, load_taxonomy())
    except Exception as exc:  # noqa: BLE001 - seeding must not break a management call
        logging.warning("Taxonomy seed for %s failed (non-fatal): %s", instance, exc)


def tenant_taxonomy_payload(instance: str) -> dict:
    """Return the tenant taxonomy while honouring a deliberately empty one."""
    if db.taxonomy_is_seeded(instance):
        return db.get_taxonomy(instance) or {"instance": instance, "domains": {}}

    from ..domain_tags.service import get_taxonomy_for_api

    return get_taxonomy_for_api(instance)
