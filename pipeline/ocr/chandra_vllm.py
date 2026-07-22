"""Chandra OCR 2 provider (HF HTTP API or remote vLLM)."""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Callable, Optional

import fitz
import httpx
from PIL import Image

from .base import OcrConfig, OcrProvider, PageDict

logger = logging.getLogger(__name__)

OCR_LAYOUT_PROMPT_MARKER = "ocr_layout"


def _default_log(level: str, message: str, *args) -> None:
    log_fn = getattr(logger, level, logger.info)
    log_fn(message, *args)


def _pdf_pages_as_images(pdf_path: str, start_idx: int, end_idx: int, dpi: int = 192) -> list[Image.Image]:
    src = fitz.open(pdf_path)
    images: list[Image.Image] = []
    try:
        for idx in range(start_idx, end_idx):
            page = src.load_page(idx)
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            images.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    finally:
        src.close()
    return images


def _image_to_base64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


class ChandraVllmOcrProvider(OcrProvider):
    name = "chandra"

    def __init__(self, config: OcrConfig):
        if not config.endpoint and not config.api_url:
            raise ValueError("CHANDRA_VLLM_BASE_URL or CHANDRA_OCR_API_URL is required for chandra OCR")
        self.config = config
        self.model = config.model or "chandra"
        self.inference_mode = (config.inference_mode or "hf").strip().lower()
        self.api_url = (config.api_url or "").strip()
        if not self.api_url:
            base = (config.endpoint or "").rstrip("/")
            if self.inference_mode == "hf":
                self.api_url = f"{base}/ocr/pages" if base.endswith("/v1") else f"{base}/v1/ocr/pages"
            else:
                self.api_url = base if base.endswith("/v1") else f"{base}/v1"

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
            "Running Chandra OCR (%s) for pages %s-%s of %s via %s",
            self.inference_mode,
            start_idx + 1,
            end_idx,
            Path(pdf_path).name,
            self.api_url,
        )

        if self.inference_mode == "hf":
            return self._process_hf(pdf_path, start_idx, end_idx, emit)
        return self._process_vllm(pdf_path, start_idx, end_idx, emit)

    def _process_hf(
        self,
        pdf_path: str,
        start_idx: int,
        end_idx: int,
        emit: Callable[..., None],
    ) -> list[PageDict]:
        images = _pdf_pages_as_images(pdf_path, start_idx, end_idx, dpi=self.config.image_dpi)
        payload = {
            "images": [_image_to_base64(image) for image in images],
            "prompt_type": OCR_LAYOUT_PROMPT_MARKER,
            "max_output_tokens": self.config.max_output_tokens,
        }
        timeout = max(120.0, self.config.request_timeout_seconds)
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(self.api_url, json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f"Chandra OCR unreachable at {self.api_url} ({exc}). "
                "Start the HF server: python scripts/chandra_hf_server.py "
                "(GPU + chandra-ocr + torch required), or for local unblock without GPU: "
                "python scripts/mock_chandra_ocr_server.py — "
                "or set OCR_PROVIDER=mock / OCR_PROVIDER=pypdf and restart the worker."
            ) from exc

        pages: list[PageDict] = []
        for offset, item in enumerate(body.get("pages", [])):
            if item.get("error"):
                emit(
                    "warning",
                    "Chandra HF OCR error on page %s of %s",
                    start_idx + offset + 1,
                    Path(pdf_path).name,
                )
            pages.append(
                {
                    "page_number": start_idx + offset + 1,
                    "original_markdown": item.get("markdown") or "",
                    "edited_markdown": None,
                    "is_reviewed": False,
                    "reviewer_notes": None,
                }
            )
        return pages

    def _process_vllm(
        self,
        pdf_path: str,
        start_idx: int,
        end_idx: int,
        emit: Callable[..., None],
    ) -> list[PageDict]:
        try:
            from chandra.input import load_file
            from chandra.model import InferenceManager
            from chandra.model.schema import BatchInputItem
        except ImportError as exc:
            raise RuntimeError(
                "chandra-ocr package is required for OCR_PROVIDER=chandra. "
                "Install with: pip install chandra-ocr"
            ) from exc

        page_range = f"{start_idx + 1}-{end_idx}"
        images = load_file(pdf_path, {"page_range": page_range})
        batch = [BatchInputItem(image=image, prompt_type=OCR_LAYOUT_PROMPT_MARKER) for image in images]
        manager = InferenceManager(method="vllm")
        results = manager.generate(
            batch,
            max_output_tokens=self.config.max_output_tokens,
            max_workers=max(1, self.config.max_workers),
            vllm_api_base=self.api_url,
        )

        pages: list[PageDict] = []
        for offset, result in enumerate(results):
            if result.error:
                emit(
                    "warning",
                    "Chandra OCR error on page %s of %s",
                    start_idx + offset + 1,
                    Path(pdf_path).name,
                )
            pages.append(
                {
                    "page_number": start_idx + offset + 1,
                    "original_markdown": result.markdown or "",
                    "edited_markdown": None,
                    "is_reviewed": False,
                    "reviewer_notes": None,
                }
            )
        return pages
