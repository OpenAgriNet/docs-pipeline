"""OCR service layer and provider selection."""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from pypdf import PdfReader

from .base import OcrConfig, OcrProvider, PageDict
from .chandra_vllm import ChandraVllmOcrProvider

PROVIDERS: dict[str, type[OcrProvider]] = {
    "chandra": ChandraVllmOcrProvider,
    "chandra_vllm": ChandraVllmOcrProvider,
}

logger = logging.getLogger(__name__)


def load_ocr_config() -> OcrConfig:
    provider = os.environ.get("OCR_PROVIDER", "chandra").strip().lower()
    model = os.environ.get("OCR_MODEL", "chandra").strip() or "chandra"
    endpoint = os.environ.get("CHANDRA_VLLM_BASE_URL", "").strip()
    api_url = os.environ.get("CHANDRA_OCR_API_URL", "").strip()
    inference_mode = os.environ.get("CHANDRA_INFERENCE_MODE", "hf").strip().lower()
    max_split_pages = int(os.environ.get("OCR_MAX_SPLIT_PAGES", "40"))
    segment_pages = int(os.environ.get("OCR_SEGMENT_PAGES", "20"))
    max_output_tokens = int(os.environ.get("CHANDRA_MAX_OUTPUT_TOKENS", "12288"))
    max_workers = int(os.environ.get("CHANDRA_OCR_MAX_WORKERS", "4"))
    image_dpi = int(os.environ.get("CHANDRA_IMAGE_DPI", "192"))
    request_timeout_seconds = float(os.environ.get("CHANDRA_REQUEST_TIMEOUT_SECONDS", "300"))
    return OcrConfig(
        provider=provider,
        model=model,
        api_key="",
        endpoint=endpoint,
        api_url=api_url,
        inference_mode=inference_mode,
        max_split_pages=max_split_pages,
        segment_pages=segment_pages,
        max_output_tokens=max_output_tokens,
        max_workers=max_workers,
        image_dpi=image_dpi,
        request_timeout_seconds=request_timeout_seconds,
    )


def get_ocr_provider(config: Optional[OcrConfig] = None) -> OcrProvider:
    config = config or load_ocr_config()
    provider_cls = PROVIDERS.get(config.provider)
    if not provider_cls:
        supported = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unsupported OCR provider '{config.provider}'. Supported: {supported}")
    return provider_cls(config)


def _activity_log(level: str, message: str, *args) -> None:
    try:
        from temporalio import activity

        log_fn = getattr(activity.logger, level, activity.logger.info)
        log_fn(message, *args)
    except Exception:
        log_fn = getattr(logger, level, logger.info)
        log_fn(message, *args)


def _finalize_pages(pages: list[PageDict], clean_text: Callable[[str], str]) -> list[PageDict]:
    finalized: list[PageDict] = []
    for page in pages:
        raw = page.get("original_markdown", "") or ""
        finalized.append(
            {
                **page,
                "original_markdown": clean_text(raw),
            }
        )
    return finalized


def ocr_pdf(local_pdf_path: str, clean_text: Callable[[str], str]) -> list[PageDict]:
    config = load_ocr_config()
    provider = get_ocr_provider(config)
    reader = PdfReader(local_pdf_path)
    pages = provider.process_pdf_range(local_pdf_path, 0, len(reader.pages), log=_activity_log)
    pages = _finalize_pages(pages, clean_text)
    _activity_log("info", "OCR complete (%s): %s pages", config.provider, len(pages))
    return pages


def ocr_pdf_in_segments(
    local_pdf_path: str,
    segment_pages: int,
    clean_text: Callable[[str], str],
    on_segment_complete=None,
    completed_page_numbers: set[int] | None = None,
) -> list[PageDict]:
    config = load_ocr_config()
    provider = get_ocr_provider(config)
    completed_page_numbers = completed_page_numbers or set()
    total_pages = len(PdfReader(local_pdf_path).pages)
    segment_pages = max(1, segment_pages or config.segment_pages)
    all_pages: list[PageDict] = []

    for start_idx in range(0, total_pages, segment_pages):
        end_idx = min(total_pages, start_idx + segment_pages)
        segment_numbers = set(range(start_idx + 1, end_idx + 1))
        if segment_numbers.issubset(completed_page_numbers):
            _activity_log(
                "info",
                "Skipping already-persisted OCR segment pages %s-%s for %s",
                start_idx + 1,
                end_idx,
                local_pdf_path,
            )
            continue

        _activity_log(
            "info",
            "Running OCR (%s) for segment pages %s-%s of %s",
            config.provider,
            start_idx + 1,
            end_idx,
            local_pdf_path,
        )
        segment_pages_result = provider.process_pdf_range(
            local_pdf_path,
            start_idx,
            end_idx,
            log=_activity_log,
        )
        segment_pages_result = _finalize_pages(segment_pages_result, clean_text)
        if on_segment_complete:
            on_segment_complete(segment_pages_result, total_pages)
        all_pages.extend(segment_pages_result)

    return all_pages
