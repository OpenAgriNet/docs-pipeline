"""Turn a published advisory ``webhook_records`` row into Qdrant chunk records.

One advisory entry (``popList`` item) becomes one chunk when its whole text fits the
embedding model's token limit, otherwise up to three chunks that each carry the advice
(overview + advice, symptoms + advice, conditions + advice). The stored ``text`` is
always the complete chunk text; the embedding model reads only its first tokens.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Optional

MAX_EMBEDDING_TOKENS = 512

# (section, label) for the question-side fields, in display order.
_FIELD_SECTIONS = (
    ("overview", "Overview"),
    ("symptoms", "Symptoms"),
    ("conditions", "Conditions"),
)


def clean_value(value: object) -> str:
    """Return a trimmed string with surrounding quote characters removed ('' for None)."""
    if value is None:
        return ""
    return str(value).strip().strip('"').strip()


def _clean_list(value: object) -> str:
    """Join the non-empty cleaned items of a list (or a single value) with spaces."""
    if value is None:
        return ""
    items = value if isinstance(value, (list, tuple)) else [value]
    return " ".join(part for part in (clean_value(item) for item in items) if part)


def _is_published(row: dict) -> bool:
    return row.get("is_published") in (1, True) and clean_value(row.get("status")) == "Published"


def _header(title: str, crop: str, problem_type: str, season: str) -> list[str]:
    lines = [title] if title else []
    parts = []
    if crop:
        parts.append(f"Crop: {crop}")
    if problem_type:
        parts.append(f"Problem type: {problem_type}")
    if season:
        parts.append(f"Season: {season}")
    if parts:
        lines.append(" | ".join(parts))
    return lines


def _chunk_id(doc_id: str, pop_id: object, section: str) -> str:
    return hashlib.md5(f"{doc_id}:{pop_id}:{section}".encode("utf-8")).hexdigest()


def build_chunks(
    row: dict,
    count_tokens: Callable[[str], int],
    max_tokens: int = MAX_EMBEDDING_TOKENS,
) -> list[dict]:
    """Build Qdrant records for one ``webhook_records`` row (empty list if unpublished)."""
    if not _is_published(row):
        return []
    payload = row.get("payload") or {}
    pop_list = payload.get("popList")
    if not isinstance(pop_list, list):
        return []

    doc_id = str(row["id"])
    title = clean_value(row.get("title")) or clean_value(payload.get("title"))
    crop = clean_value(payload.get("commodityName"))
    base = {
        "doc_id": doc_id,
        "workflow_id": "",
        "type": "document",
        "document_kind": "advisory",
        "source": "webhook",
        "title_en": title,
        "slug": clean_value(row.get("slug")),
        "external_id": clean_value(row.get("external_id")),
        "crop": crop,
        "domain": clean_value(payload.get("domainName")),
        "subdomain": clean_value(payload.get("subdomainName")),
        "institute": clean_value(payload.get("instituteName")),
        "creator": clean_value(payload.get("creatorName")),
        "updated_at": clean_value(row.get("updated_at")),
    }

    records: list[dict] = []

    def add(pop: dict, index: int, section: str, text: str, meta: dict) -> None:
        pop_id = pop.get("popID")
        key = pop_id if pop_id is not None else f"idx{index}"
        chunk_id = _chunk_id(doc_id, key, section)
        records.append(
            {
                **base,
                **meta,
                "_id": chunk_id,
                "chunk_id": chunk_id,
                "pop_id": pop_id,
                "section": section,
                "chunk_num": len(records),
                "text": text,
                "text_for_embedding": text,
            }
        )

    for index, pop in enumerate(pop_list):
        if not isinstance(pop, dict) or clean_value(pop.get("status")) != "Published":
            continue
        problem_type = clean_value(pop.get("problemType"))
        season = clean_value(pop.get("season"))
        meta = {
            "problem_type": problem_type,
            "problem": clean_value(pop.get("problemDetails")),
            "season": season,
            "start_period": clean_value(pop.get("startPeriod")),
            "end_period": clean_value(pop.get("endPeriod")),
        }
        header = _header(title, crop, problem_type, season)
        fields = {
            "overview": clean_value(pop.get("description")),
            "symptoms": _clean_list(pop.get("applicableSymptoms")),
            "conditions": _clean_list(pop.get("applicableCondition")),
        }
        advisory = clean_value(pop.get("advisory"))
        labelled = [(label, fields[section]) for section, label in _FIELD_SECTIONS if fields[section]]
        if advisory:
            labelled.append(("Advisory", advisory))
        if not labelled:
            continue

        full_text = "\n".join(header + [f"{label}: {body}" for label, body in labelled])
        if count_tokens(full_text) <= max_tokens:
            add(pop, index, "full", full_text, meta)
            continue

        others = [(section, label) for section, label in _FIELD_SECTIONS if fields[section]]
        if not others:
            add(pop, index, "advisory", full_text, meta)
            continue
        for section, label in others:
            lines = header + [f"{label}: {fields[section]}"]
            if advisory:
                lines.append(f"Advisory: {advisory}")
            add(pop, index, section, "\n".join(lines), meta)

    return records
