"""Shared page-unit extraction and reconstruction helpers for chunking."""

from __future__ import annotations

import re

from .base import ChunkCandidate, ChunkingConfig, count_tokens


def best_page_text(page: dict) -> str:
    return (
        page.get("edited_translation")
        or page.get("translated_markdown")
        or page.get("edited_markdown")
        or page.get("original_markdown", "")
    )


def detect_section_title(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:200]
    return ""


def is_reference_section(text: str) -> bool:
    lowered = text.lower()
    citation_patterns = [
        r"^\s*references?\s*$",
        r"^\s*bibliography\s*$",
        r"^\s*cited\s+by\s*$",
        r"^\s*works\s+cited\s*$",
        r"\bet al\.\b",
        r"\bdoi:\b",
        r"https?://\S+",
        r"\[[0-9]+\]",
    ]
    if any(re.search(pattern, lowered, re.IGNORECASE | re.MULTILINE) for pattern in citation_patterns):
        return True
    return False


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def split_page_into_units(page_number: int, text: str, config: ChunkingConfig) -> list[dict]:
    if not text.strip():
        return []

    # Keep units small enough that the LLM only has to choose boundaries, not rewrite text.
    units: list[dict] = []
    cursor = 0
    raw_blocks = [block for block in re.split(r"\n\s*\n+", text) if block and block.strip()]
    if not raw_blocks:
        raw_blocks = [text]

    max_chars = max(600, int(config.max_chunk_tokens * 4.2))
    for block in raw_blocks:
        block_cursor = text.find(block, cursor)
        if block_cursor == -1:
            block_cursor = cursor
        start = 0
        while start < len(block):
            piece = block[start:start + max_chars]
            piece_start = block_cursor + start
            piece_end = piece_start + len(piece)
            piece_text = piece.strip()
            if piece_text:
                units.append(
                    {
                        "text": piece_text,
                        "page_number": page_number,
                        "source_spans": [{"page_number": page_number, "start_char": piece_start, "end_char": piece_end}],
                        "token_count": count_tokens(piece_text),
                        "section_title": detect_section_title(piece_text),
                        "content_type": "heading" if piece_text.lstrip().startswith("#") else "body",
                    }
                )
            start += max_chars
        cursor = max(cursor, block_cursor + len(block))
    return units


def merge_units(units: list[dict], section_title_hint: str = "", is_reference_hint: bool | None = None) -> ChunkCandidate:
    text_parts = []
    source_spans = []
    source_page_numbers: list[int] = []
    section_title = section_title_hint.strip()[:200]
    content_type = "body"

    for unit in units:
        if text_parts:
            text_parts.append("\n\n")
        text_parts.append(unit["text"].strip())
        source_spans.extend(unit["source_spans"])
        if unit["page_number"] not in source_page_numbers:
            source_page_numbers.append(unit["page_number"])
        if not section_title and unit.get("section_title"):
            section_title = unit["section_title"]
        if unit.get("content_type") == "heading":
            content_type = "heading"

    text = "".join(text_parts).strip()
    return ChunkCandidate(
        text=text,
        page_start=min(source_page_numbers) if source_page_numbers else 1,
        page_end=max(source_page_numbers) if source_page_numbers else 1,
        source_page_numbers=source_page_numbers or [1],
        source_spans=source_spans,
        token_count=count_tokens(text),
        section_title=section_title or detect_section_title(text),
        content_type=content_type,
        is_reference=is_reference_section(text) if is_reference_hint is None else bool(is_reference_hint),
    )
