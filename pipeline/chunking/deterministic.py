"""Deterministic page-aware chunking fallback."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Awaitable, Callable, Optional

from .base import ChunkCandidate, ChunkingConfig, ChunkingProvider, ChunkingResult, count_tokens
from .page_units import best_page_text, is_reference_section, merge_units, normalize_text, split_page_into_units


def _select_overlap_units(units: list[dict], overlap_tokens: int) -> list[dict]:
    if overlap_tokens <= 0:
        return []
    selected: list[dict] = []
    token_total = 0
    for unit in reversed(units):
        selected.insert(0, unit)
        token_total += unit["token_count"]
        if token_total >= overlap_tokens:
            break
    return selected


def _dedupe_chunks(chunks: list[ChunkCandidate]) -> tuple[list[ChunkCandidate], list[str]]:
    warnings: list[str] = []
    deduped: list[ChunkCandidate] = []
    seen_signatures: set[tuple[int, int, str]] = set()
    for chunk in chunks:
        normalized = normalize_text(chunk.text)
        if not normalized:
            continue
        signature = (chunk.page_start, chunk.page_end, normalized[:240])
        if signature in seen_signatures:
            warnings.append(f"Dropped duplicate deterministic chunk on pages {chunk.page_start}-{chunk.page_end}")
            continue
        if deduped:
            prev = deduped[-1]
            prev_normalized = normalize_text(prev.text)
            if (
                prev.page_start == chunk.page_start
                and prev.page_end == chunk.page_end
                and normalized in prev_normalized
                and len(normalized) < len(prev_normalized)
            ):
                warnings.append(f"Dropped contained deterministic chunk on pages {chunk.page_start}-{chunk.page_end}")
                continue
        deduped.append(chunk)
        seen_signatures.add(signature)
    return deduped, warnings


def _merge_adjacent_chunks(chunks: list[ChunkCandidate], config: ChunkingConfig) -> tuple[list[ChunkCandidate], list[str]]:
    warnings: list[str] = []
    if not chunks:
        return [], warnings

    merged: list[ChunkCandidate] = [chunks[0]]
    merge_limit = int(config.max_chunk_tokens * 1.8)
    for chunk in chunks[1:]:
        prev = merged[-1]
        compatible_type = prev.content_type == chunk.content_type or "body" in {prev.content_type, chunk.content_type}
        compatible_reference = prev.is_reference == chunk.is_reference
        adjacent_pages = chunk.page_start <= prev.page_end + 1
        combined_tokens = prev.token_count + chunk.token_count
        same_page = prev.page_start == prev.page_end == chunk.page_start == chunk.page_end
        if compatible_type and compatible_reference and adjacent_pages and (same_page or combined_tokens <= merge_limit):
            merged_text = f"{prev.text.rstrip()}\n\n{chunk.text.lstrip()}".strip()
            merged[-1] = ChunkCandidate(
                text=merged_text,
                page_start=min(prev.page_start, chunk.page_start),
                page_end=max(prev.page_end, chunk.page_end),
                source_page_numbers=sorted(set(prev.source_page_numbers + chunk.source_page_numbers)),
                source_spans=prev.source_spans + chunk.source_spans,
                token_count=count_tokens(merged_text),
                section_title=prev.section_title or chunk.section_title,
                content_type=prev.content_type if prev.content_type != "body" else chunk.content_type,
                is_reference=prev.is_reference,
                metadata={**prev.metadata, "merged_adjacent": True},
            )
            warnings.append(f"Merged adjacent deterministic chunks on pages {prev.page_start}-{prev.page_end} and {chunk.page_start}-{chunk.page_end}")
        else:
            merged.append(chunk)
    return merged, warnings


class DeterministicChunkingProvider(ChunkingProvider):
    name = "deterministic"

    async def chunk_document(
        self,
        pages: list[dict],
        config: ChunkingConfig,
        progress_callback: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
    ) -> ChunkingResult:
        units: list[dict] = []
        for page in pages:
            units.extend(split_page_into_units(page.get("page_number", 1), best_page_text(page), config))

        chunks: list[ChunkCandidate] = []
        current_units: list[dict] = []
        current_tokens = 0

        def flush_chunk(force: bool = False) -> None:
            nonlocal current_units, current_tokens
            if not current_units:
                return
            candidate = merge_units(current_units)
            if force or candidate.token_count >= config.min_chunk_tokens or not chunks:
                chunks.append(candidate)
            overlap_units = _select_overlap_units(current_units, config.chunk_overlap_tokens)
            current_units = overlap_units
            current_tokens = sum(unit["token_count"] for unit in current_units)

        total_units = len(units)
        total_pages = max(1, len(pages))
        processed_units = 0

        if progress_callback:
            await progress_callback(
                {
                    "provider": self.name,
                    "units_processed": 0,
                    "units_total": total_units,
                    "pages_processed": 0,
                    "pages_total": total_pages,
                    "chunks_emitted": 0,
                    "percent": 0.0,
                }
            )

        for unit in units:
            unit_pages = {u["page_number"] for u in current_units}
            proposed_pages = unit_pages | {unit["page_number"]}
            would_exceed_pages = len(proposed_pages) > max(1, config.max_pages_per_chunk)
            would_exceed_tokens = current_units and (current_tokens + unit["token_count"] > config.max_chunk_tokens)

            if would_exceed_pages or would_exceed_tokens:
                flush_chunk(force=current_tokens >= config.min_chunk_tokens)

            current_units.append(unit)
            current_tokens += unit["token_count"]
            processed_units += 1

            if progress_callback and (processed_units % 8 == 0 or processed_units == total_units):
                pages_processed = min(total_pages, max(0, unit.get("page_number", 0)))
                percent = (processed_units / total_units * 100.0) if total_units else 100.0
                await progress_callback(
                    {
                        "provider": self.name,
                        "units_processed": processed_units,
                        "units_total": total_units,
                        "pages_processed": pages_processed,
                        "pages_total": total_pages,
                        "chunks_emitted": len(chunks),
                        "percent": percent,
                    }
                )

        flush_chunk(force=True)

        filtered_chunks = [chunk for chunk in chunks if chunk.token_count >= config.min_chunk_tokens or len(chunks) == 1]
        filtered_chunks, warnings = _dedupe_chunks(filtered_chunks)
        merged_chunks, merge_warnings = _merge_adjacent_chunks(filtered_chunks, config)
        warnings.extend(merge_warnings)
        if progress_callback:
            await progress_callback(
                {
                    "provider": self.name,
                    "units_processed": total_units,
                    "units_total": total_units,
                    "pages_processed": total_pages,
                    "pages_total": total_pages,
                    "chunks_emitted": len(merged_chunks),
                    "percent": 100.0,
                }
            )
        stats = {
            "page_count": len(pages),
            "unit_count": len(units),
            "chunk_count": len(merged_chunks),
        }
        return ChunkingResult(
            chunks=merged_chunks,
            provider=self.name,
            model=config.model,
            config=replace(config, provider=self.name),
            warnings=warnings,
            stats=stats,
        )
