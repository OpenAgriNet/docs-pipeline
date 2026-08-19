"""Post-chunking guards so no chunk can exceed token or Marqo record limits."""

from __future__ import annotations

import json

from langchain_text_splitters import RecursiveCharacterTextSplitter

from .base import ChunkCandidate, ChunkingConfig, ChunkingResult, count_tokens

# Marqo rejects records above 100KB; keep headroom for metadata fields at ingest.
MARQO_MAX_DOCUMENT_BYTES = 98_000
E5_PASSAGE_PREFIX = "passage:"


def estimate_marqo_tensor_payload_bytes(text: str) -> int:
    """Approximate bytes for the two largest Marqo text fields on a chunk record."""
    payload = {"text": text, "text_for_embedding": f"{E5_PASSAGE_PREFIX}{text}"}
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def enforce_max_chunk_tokens(result: ChunkingResult) -> ChunkingResult:
    """Split any chunk larger than ``config.max_chunk_tokens``."""
    max_tokens = max(1, int(result.config.max_chunk_tokens or 450))
    overlap = max(0, min(int(result.config.chunk_overlap_tokens or 0), max_tokens // 4))
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_tokens,
        chunk_overlap=overlap,
        length_function=count_tokens,
    )

    kept: list[ChunkCandidate] = []
    split_count = 0
    for chunk in result.chunks:
        if chunk.token_count <= max_tokens:
            kept.append(chunk)
            continue
        parts = splitter.split_text(chunk.text or "")
        if len(parts) <= 1:
            text = chunk.text or ""
            step = max(1, len(text) // max(1, (chunk.token_count // max_tokens) + 1))
            parts = [text[i : i + step] for i in range(0, len(text), step) if text[i : i + step].strip()]
        for part in parts:
            part = part.strip()
            if not part:
                continue
            kept.append(_clone_chunk(chunk, part))
            split_count += 1

    if split_count:
        result.warnings.append(
            f"Split {split_count} oversized chunk(s) to respect max_chunk_tokens={max_tokens}"
        )
    result.chunks = kept
    return result


def enforce_marqo_byte_limit(
    result: ChunkingResult,
    max_bytes: int = MARQO_MAX_DOCUMENT_BYTES,
) -> ChunkingResult:
    """Split chunks whose text payload would exceed Marqo's per-document byte cap."""
    kept: list[ChunkCandidate] = []
    split_count = 0

    for chunk in result.chunks:
        text = (chunk.text or "").strip()
        if not text:
            continue
        if estimate_marqo_tensor_payload_bytes(text) <= max_bytes:
            kept.append(chunk)
            continue

        # Binary split until each part fits the Marqo payload budget.
        pending = [text]
        parts: list[str] = []
        while pending:
            piece = pending.pop()
            if estimate_marqo_tensor_payload_bytes(piece) <= max_bytes:
                parts.append(piece)
                continue
            mid = max(1, len(piece) // 2)
            left, right = piece[:mid].strip(), piece[mid:].strip()
            if not left or not right:
                # Single char still too large — hard truncate as last resort.
                parts.append(piece[: max(1, max_bytes // 4)])
                split_count += 1
                continue
            pending.extend([right, left])
            split_count += 1

        for part in parts:
            if part.strip():
                kept.append(_clone_chunk(chunk, part.strip()))

    if split_count:
        result.warnings.append(
            f"Split {split_count} chunk part(s) to respect Marqo byte limit={max_bytes}"
        )
    result.chunks = kept
    return result


def enforce_chunk_limits(result: ChunkingResult) -> ChunkingResult:
    """Apply token and Marqo byte guards in order."""
    return enforce_marqo_byte_limit(enforce_max_chunk_tokens(result))


def _clone_chunk(chunk: ChunkCandidate, text: str) -> ChunkCandidate:
    return ChunkCandidate(
        text=text,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        source_page_numbers=list(chunk.source_page_numbers),
        source_spans=list(chunk.source_spans),
        token_count=count_tokens(text),
        section_title=chunk.section_title,
        content_type=chunk.content_type,
        is_reference=chunk.is_reference,
        metadata=dict(chunk.metadata),
    )
