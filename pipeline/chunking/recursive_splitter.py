"""Recursive character text splitter chunking provider (no LLM calls).

Replaces the previous LLM-based (Qwen/OpenAI-compatible vLLM) chunk-boundary
provider. Pages are cleaned of HTML/LaTeX/table markup noise, joined into
per-window text, and split with `RecursiveCharacterTextSplitter` using a
token-aware length function so chunk boundaries respect paragraph/sentence
structure instead of requiring an LLM call.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Awaitable, Callable, Optional

from langchain_text_splitters import RecursiveCharacterTextSplitter

from .base import ChunkCandidate, ChunkingConfig, ChunkingProvider, ChunkingResult, count_tokens
from .page_units import best_page_text, detect_section_title, is_reference_section


def clean_html_tags(text: str) -> str:
    """Remove HTML tags from text."""
    if not isinstance(text, str):
        return text
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def format_table_content(text: str) -> str:
    """Clean and format table content from markdown."""
    if not isinstance(text, str):
        return text

    lines = text.split("\n")
    cleaned_lines = []

    for line in lines:
        # Skip lines with only pipes and spaces
        if re.match(r"^[\s\|]*$", line):
            continue
        # Skip separator lines (lines with only dashes and pipes)
        if re.match(r"^[\s\|\-\:]*$", line):
            continue

        # Clean up excessive pipes
        line = re.sub(r"\|\s*\|", "|", line)
        line = re.sub(r"^\|\s*", "", line)
        line = re.sub(r"\s*\|$", "", line)
        line = re.sub(r"\s+", " ", line).strip()

        if line:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def clean_latex_notation(text: str) -> str:
    """Remove LaTeX math notation and reference markers."""
    if not isinstance(text, str):
        return text

    # Remove LaTeX reference markers like [^0], [^1], etc.
    text = re.sub(r"\[\^[0-9]+\]", "", text)

    # Remove LaTeX math expressions like ${ }^{1}$, $^{2}$, etc.
    text = re.sub(r"\$\s*\{\s*\}\s*\^\{[0-9]+\}\s*\$", "", text)
    text = re.sub(r"\$\s*\^\{[0-9]+\}\s*\$", "", text)

    # Remove standalone LaTeX math mode markers
    text = re.sub(r"\$\s*\$", "", text)

    # Remove LaTeX commands (basic ones)
    text = re.sub(r"\\[a-zA-Z]+\{[^}]*\}", "", text)
    text = re.sub(r"\\[a-zA-Z]+", "", text)

    # Remove LaTeX special characters
    text = re.sub(r"[\\{}]", "", text)

    return text


def clean_and_format_text(text: str) -> str:
    """Comprehensive text cleaning and formatting."""
    if not isinstance(text, str):
        return text

    # Clean HTML tags
    text = clean_html_tags(text)

    # Clean LaTeX notation
    text = clean_latex_notation(text)

    # Format table content
    text = format_table_content(text)

    # Remove multiple newlines (keep max 2 consecutive)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Remove multiple spaces and tabs
    text = re.sub(r"[ \t]+", " ", text)

    # Remove leading/trailing whitespace from each line
    lines = text.split("\n")
    lines = [line.strip() for line in lines]
    text = "\n".join(lines)

    # Remove empty lines (keep single empty lines for paragraph breaks)
    text = re.sub(r"\n\s*\n\s*\n", "\n\n", text)

    # Final cleanup
    text = re.sub(r"[ \t]+", " ", text)  # Multiple spaces to single space
    text = re.sub(r"\n ", "\n", text)  # Remove space after newline
    text = re.sub(r" \n", "\n", text)  # Remove space before newline

    return text.strip()


def chunk_text_with_tokens(
    text: str,
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_chunk_size: int = 100,
) -> list[str]:
    """Chunk text using RecursiveCharacterTextSplitter with proper token counting."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=count_tokens,
        separators=["\n\n", "\n", ".", " ", ""],
    )

    chunks = splitter.split_text(text)

    filtered_chunks = []
    for chunk in chunks:
        token_count = count_tokens(chunk)
        if min_chunk_size <= token_count <= chunk_size:
            filtered_chunks.append(chunk)
        elif token_count > chunk_size:
            sub_chunks = splitter.split_text(chunk)
            for sub_chunk in sub_chunks:
                sub_token_count = count_tokens(sub_chunk)
                if sub_token_count >= min_chunk_size:
                    filtered_chunks.append(sub_chunk)

    return filtered_chunks


def _build_window_text(window: list[dict]) -> tuple[str, list[tuple[int, int, int]]]:
    """Join cleaned page texts for a window, tracking (start, end, page_number) offsets."""
    parts: list[str] = []
    offsets: list[tuple[int, int, int]] = []
    cursor = 0
    for page in window:
        cleaned = clean_and_format_text(best_page_text(page))
        if not cleaned:
            continue
        if parts:
            parts.append("\n\n")
            cursor += 2
        start = cursor
        parts.append(cleaned)
        cursor += len(cleaned)
        offsets.append((start, cursor, page.get("page_number", 1)))
    return "".join(parts), offsets


def _pages_for_span(offsets: list[tuple[int, int, int]], start: int, end: int) -> list[int]:
    pages = [page_number for (span_start, span_end, page_number) in offsets if span_start < end and span_end > start]
    if pages:
        return sorted(set(pages))
    return [offsets[0][2]] if offsets else [1]


def _spans_for_range(offsets: list[tuple[int, int, int]], start: int, end: int) -> list[dict[str, int]]:
    spans = []
    for span_start, span_end, page_number in offsets:
        if span_start < end and span_end > start:
            spans.append(
                {
                    "page_number": page_number,
                    "start_char": max(start, span_start) - span_start,
                    "end_char": min(end, span_end) - span_start,
                }
            )
    return spans


class RecursiveSplitterChunkingProvider(ChunkingProvider):
    """Splits page text into chunks with RecursiveCharacterTextSplitter (no LLM calls)."""

    name = "recursive_splitter"

    async def chunk_document(
        self,
        pages: list[dict],
        config: ChunkingConfig,
        progress_callback: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
    ) -> ChunkingResult:
        warnings: list[str] = []
        chunks: list[ChunkCandidate] = []
        page_window_size = max(1, config.page_window_size)
        total_pages = max(1, len(pages))
        total_windows = max(1, (len(pages) + page_window_size - 1) // page_window_size)
        windows_done = 0

        if progress_callback:
            await progress_callback(
                {
                    "provider": self.name,
                    "windows_processed": 0,
                    "windows_total": total_windows,
                    "pages_processed": 0,
                    "pages_total": total_pages,
                    "chunks_emitted": 0,
                    "percent": 0.0,
                }
            )

        for window_start in range(0, len(pages), page_window_size):
            window = pages[window_start: window_start + page_window_size]
            window_text, offsets = _build_window_text(window)

            if window_text.strip():
                raw_chunks = chunk_text_with_tokens(
                    window_text,
                    chunk_size=config.max_chunk_tokens,
                    chunk_overlap=config.chunk_overlap_tokens,
                    min_chunk_size=config.min_chunk_tokens,
                )

                search_from = 0
                for raw_chunk in raw_chunks:
                    text = raw_chunk.strip()
                    if not text:
                        continue
                    found = window_text.find(raw_chunk, search_from)
                    if found == -1:
                        found = window_text.find(raw_chunk)
                    start = found if found != -1 else 0
                    end = start + len(raw_chunk)
                    if found != -1:
                        search_from = start

                    page_numbers = _pages_for_span(offsets, start, end)
                    source_spans = _spans_for_range(offsets, start, end) or [
                        {"page_number": page_numbers[0], "start_char": 0, "end_char": len(text)}
                    ]
                    chunks.append(
                        ChunkCandidate(
                            text=text,
                            page_start=min(page_numbers),
                            page_end=max(page_numbers),
                            source_page_numbers=page_numbers,
                            source_spans=source_spans,
                            token_count=count_tokens(text),
                            section_title=detect_section_title(text),
                            content_type="heading" if text.lstrip().startswith("#") else "body",
                            is_reference=is_reference_section(text),
                        )
                    )

            windows_done += 1
            if progress_callback:
                pages_processed = min(total_pages, window_start + len(window))
                percent = windows_done / total_windows * 100.0
                await progress_callback(
                    {
                        "provider": self.name,
                        "windows_processed": windows_done,
                        "windows_total": total_windows,
                        "pages_processed": pages_processed,
                        "pages_total": total_pages,
                        "chunks_emitted": len(chunks),
                        "percent": percent,
                    }
                )

        stats = {"page_count": len(pages), "chunk_count": len(chunks)}
        return ChunkingResult(
            chunks=chunks,
            provider=self.name,
            model=config.model,
            config=replace(config, provider=self.name),
            warnings=warnings,
            stats=stats,
        )
