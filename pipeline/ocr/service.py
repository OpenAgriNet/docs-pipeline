"""OCR service layer and provider selection."""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from pypdf import PdfReader

from .base import OcrConfig, OcrProvider, PageDict
from .chandra_vllm import ChandraVllmOcrProvider
from .mistral_ocr import MistralOcrProvider
from .mock import MockOcrProvider
from .text_layer import group_contiguous_ranges, split_pdf_range

PROVIDERS: dict[str, type[OcrProvider]] = {
    "mistral": MistralOcrProvider,
    "mistral_ocr": MistralOcrProvider,
    "chandra": ChandraVllmOcrProvider,
    "chandra_vllm": ChandraVllmOcrProvider,
    # Local dev: embedded PDF text only (no external OCR).
    "mock": MockOcrProvider,
    "pypdf": MockOcrProvider,
}

logger = logging.getLogger(__name__)


def load_ocr_config() -> OcrConfig:
    provider = os.environ.get("OCR_PROVIDER", "mistral").strip().lower()
    if provider in {"mistral", "mistral_ocr"}:
        model = (
            os.environ.get("OCR_MODEL")
            or os.environ.get("MISTRAL_OCR_MODEL")
            or "mistral-ocr-latest"
        ).strip()
        api_key = (os.environ.get("MISTRAL_API_KEY") or "").strip()
        api_url = (os.environ.get("MISTRAL_OCR_API_URL") or "https://api.mistral.ai/v1/ocr").strip()
        endpoint = ""
        inference_mode = "api"
        max_output_tokens = int(os.environ.get("OCR_MAX_OUTPUT_TOKENS", "12288"))
        max_workers = int(os.environ.get("OCR_MAX_WORKERS", "2"))
        image_dpi = int(os.environ.get("OCR_IMAGE_DPI", "192"))
        request_timeout_seconds = float(os.environ.get("OCR_REQUEST_TIMEOUT_SECONDS", "300"))
    else:
        model = (os.environ.get("OCR_MODEL") or "chandra").strip() or "chandra"
        api_key = ""
        endpoint = os.environ.get("CHANDRA_VLLM_BASE_URL", "").strip()
        api_url = os.environ.get("CHANDRA_OCR_API_URL", "").strip()
        inference_mode = os.environ.get("CHANDRA_INFERENCE_MODE", "hf").strip().lower()
        max_output_tokens = int(os.environ.get("CHANDRA_MAX_OUTPUT_TOKENS", "12288"))
        max_workers = int(os.environ.get("CHANDRA_OCR_MAX_WORKERS", "4"))
        image_dpi = int(os.environ.get("CHANDRA_IMAGE_DPI", "192"))
        request_timeout_seconds = float(os.environ.get("CHANDRA_REQUEST_TIMEOUT_SECONDS", "300"))

    max_split_pages = int(os.environ.get("OCR_MAX_SPLIT_PAGES", "40"))
    segment_pages = int(os.environ.get("OCR_SEGMENT_PAGES", "20"))
    return OcrConfig(
        provider=provider,
        model=model,
        api_key=api_key,
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


def _skip_ocr_for_text_layer() -> bool:
    """Whether to bypass OCR for pages pdf-inspector can already read as text.

    Enabled by default: most PDFs (reports, invoices, legal docs) already have
    an embedded text layer and don't need an expensive OCR call at all. Set
    ``OCR_SKIP_TEXT_LAYER=false`` to always run every page through the
    configured OCR provider (old behavior).
    """
    return os.environ.get("OCR_SKIP_TEXT_LAYER", "true").strip().lower() not in {"0", "false", "no"}


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


def _process_range(
    local_pdf_path: str,
    start_idx: int,
    end_idx: int,
    config: OcrConfig,
    provider_cache: dict[str, OcrProvider],
) -> list[PageDict]:
    """OCR pages [start_idx, end_idx), skipping pages with a usable text layer."""
    if not _skip_ocr_for_text_layer():
        provider = provider_cache.setdefault("provider", get_ocr_provider(config))
        return provider.process_pdf_range(local_pdf_path, start_idx, end_idx, log=_activity_log)

    split = split_pdf_range(local_pdf_path, start_idx, end_idx, log=_activity_log)
    pages_by_number: dict[int, PageDict] = {p["page_number"]: p for p in split.text_pages}

    if split.ocr_page_numbers:
        provider = provider_cache.get("provider")
        if provider is None:
            provider = get_ocr_provider(config)
            provider_cache["provider"] = provider
        for sub_start, sub_end in group_contiguous_ranges(split.ocr_page_numbers):
            for page in provider.process_pdf_range(local_pdf_path, sub_start, sub_end, log=_activity_log):
                pages_by_number[page["page_number"]] = page

    return [
        pages_by_number[n]
        for n in range(start_idx + 1, end_idx + 1)
        if n in pages_by_number
    ]


def ocr_pdf(local_pdf_path: str, clean_text: Callable[[str], str]) -> list[PageDict]:
    config = load_ocr_config()
    reader = PdfReader(local_pdf_path)
    pages = _process_range(local_pdf_path, 0, len(reader.pages), config, {})
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
    provider_cache: dict[str, OcrProvider] = {}
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
        segment_pages_result = _process_range(local_pdf_path, start_idx, end_idx, config, provider_cache)
        segment_pages_result = _finalize_pages(segment_pages_result, clean_text)
        if on_segment_complete:
            on_segment_complete(segment_pages_result, total_pages)
        all_pages.extend(segment_pages_result)

    return all_pages
