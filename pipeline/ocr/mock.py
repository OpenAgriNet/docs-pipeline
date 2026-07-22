"""Local-dev OCR providers that do not require Chandra / GPU.

``mock`` / ``pypdf`` extract embedded PDF text with pypdf. Scanned pages with no
text layer get a short placeholder so the pipeline can still advance past OCR.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from pypdf import PdfReader

from .base import OcrConfig, OcrProvider, PageDict

logger = logging.getLogger(__name__)


def _default_log(level: str, message: str, *args) -> None:
    log_fn = getattr(logger, level, logger.info)
    log_fn(message, *args)


class MockOcrProvider(OcrProvider):
    """Extract native PDF text; no network / model dependency."""

    name = "mock"

    def __init__(self, config: OcrConfig):
        self.config = config

    def process_pdf_range(
        self,
        pdf_path: str,
        start_idx: int,
        end_idx: int,
        *,
        log: Optional[Callable[..., None]] = None,
    ) -> list[PageDict]:
        emit = log or _default_log
        if start_idx >= end_idx:
            return []

        emit(
            "info",
            "Running mock/pypdf OCR for pages %s-%s of %s",
            start_idx + 1,
            end_idx,
            Path(pdf_path).name,
        )

        reader = PdfReader(pdf_path)
        total = len(reader.pages)
        end_idx = min(end_idx, total)
        pages: list[PageDict] = []
        for idx in range(start_idx, end_idx):
            page_number = idx + 1
            try:
                raw = reader.pages[idx].extract_text() or ""
            except Exception as exc:  # noqa: BLE001 — keep pipeline moving in local dev
                emit("warning", "pypdf extract failed on page %s: %s", page_number, exc)
                raw = ""
            text = raw.strip()
            if not text:
                text = (
                    f"# Page {page_number}\n\n"
                    f"[mock-ocr] No embedded text layer on this page "
                    f"({Path(pdf_path).name}). Replace with real Chandra OCR for scanned docs."
                )
            pages.append(
                {
                    "page_number": page_number,
                    "original_markdown": text,
                    "edited_markdown": None,
                    "is_reviewed": False,
                    "reviewer_notes": None,
                }
            )
        return pages
