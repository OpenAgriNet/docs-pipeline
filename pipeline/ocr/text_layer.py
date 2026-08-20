"""Text-layer PDF extraction via pdf-inspector — skips OCR for pages that
already have a usable embedded text layer.

pdf-inspector (https://github.com/firecrawl/pdf-inspector) is a pure-Rust
library (with Python bindings) that classifies each PDF page in milliseconds
by inspecting its content streams — no OCR model, no network call. Pages it
identifies as text-based are converted straight to Markdown locally; only the
remaining pages (scanned/image-only, or pages with broken font encodings) are
routed to the configured OCR provider (Mistral / Chandra / mock).

This is a pre-filter in front of ``OcrProvider.process_pdf_range`` — it is not
itself an ``OcrProvider`` — so it lives alongside, not inside, the provider
registry in ``service.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from .base import PageDict

logger = logging.getLogger(__name__)


def _default_log(level: str, message: str, *args) -> None:
    log_fn = getattr(logger, level, logger.info)
    log_fn(message, *args)


class TextLayerSplit(NamedTuple):
    text_pages: list[PageDict]
    ocr_page_numbers: list[int]  # 1-based page numbers that still need an OCR provider


def split_pdf_range(
    pdf_path: str,
    start_idx: int,
    end_idx: int,
    *,
    log: Optional[Callable[..., None]] = None,
) -> TextLayerSplit:
    """Classify + extract pages [start_idx, end_idx) (0-based) with pdf-inspector.

    Returns Markdown pages already finished locally, plus the 1-based page
    numbers that still need to go through an OCR model. Falls back to routing
    every page to OCR if pdf-inspector is unavailable or errors out, so the
    pipeline behaves exactly as before when the library can't be used.
    """
    emit = log or _default_log
    if start_idx >= end_idx:
        return TextLayerSplit([], [])

    all_page_numbers = list(range(start_idx + 1, end_idx + 1))

    try:
        import pdf_inspector
    except ImportError:
        emit(
            "warning",
            "pdf-inspector not installed; sending all pages %s-%s of %s to OCR",
            start_idx + 1,
            end_idx,
            Path(pdf_path).name,
        )
        return TextLayerSplit([], all_page_numbers)

    page_indexes = list(range(start_idx, end_idx))
    try:
        result = pdf_inspector.extract_pages_markdown(pdf_path, pages=page_indexes)
    except Exception as exc:  # noqa: BLE001 — never let a parsing quirk break the pipeline
        emit(
            "warning",
            "pdf-inspector failed on %s (pages %s-%s): %s; falling back to OCR",
            Path(pdf_path).name,
            start_idx + 1,
            end_idx,
            exc,
        )
        return TextLayerSplit([], all_page_numbers)

    by_index = {page.page: page for page in result.pages}
    text_pages: list[PageDict] = []
    ocr_page_numbers: list[int] = []
    for idx in page_indexes:
        page_number = idx + 1
        page = by_index.get(idx)
        markdown = ((page.markdown if page else "") or "").strip()
        needs_ocr = page.needs_ocr if page is not None else True
        if page is not None and not needs_ocr and markdown:
            text_pages.append(
                {
                    "page_number": page_number,
                    "original_markdown": markdown,
                    "edited_markdown": None,
                    "is_reviewed": False,
                    "reviewer_notes": None,
                }
            )
        else:
            ocr_page_numbers.append(page_number)

    if text_pages:
        emit(
            "info",
            "pdf-inspector extracted %s/%s page(s) without OCR for %s (pages needing OCR: %s)",
            len(text_pages),
            len(all_page_numbers),
            Path(pdf_path).name,
            ocr_page_numbers or "none",
        )
    return TextLayerSplit(text_pages, ocr_page_numbers)


def group_contiguous_ranges(page_numbers: list[int]) -> list[tuple[int, int]]:
    """Collapse a list of 1-based page numbers into 0-based [start, end) ranges."""
    if not page_numbers:
        return []
    numbers = sorted(set(page_numbers))
    ranges: list[tuple[int, int]] = []
    range_start = numbers[0]
    prev = numbers[0]
    for n in numbers[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append((range_start - 1, prev))
        range_start = n
        prev = n
    ranges.append((range_start - 1, prev))
    return ranges
