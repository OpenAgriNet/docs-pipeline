"""Mistral Document OCR provider (api.mistral.ai /v1/ocr)."""

from __future__ import annotations

import base64
import logging
import os
import tempfile
from pathlib import Path
from typing import Callable, Optional

import httpx
from pypdf import PdfReader, PdfWriter

from .base import OcrConfig, OcrProvider, PageDict

logger = logging.getLogger(__name__)

DEFAULT_MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"
DEFAULT_MISTRAL_OCR_MODEL = "mistral-ocr-latest"


def _default_log(level: str, message: str, *args) -> None:
    log_fn = getattr(logger, level, logger.info)
    log_fn(message, *args)


def _extract_pdf_range(pdf_path: str, start_idx: int, end_idx: int) -> str:
    """Write pages [start_idx, end_idx) to a temporary PDF; return its path."""
    reader = PdfReader(pdf_path)
    writer = PdfWriter()
    for i in range(start_idx, min(end_idx, len(reader.pages))):
        writer.add_page(reader.pages[i])
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    with open(tmp_path, "wb") as out:
        writer.write(out)
    return tmp_path


def _encode_pdf_data_uri(pdf_path: str) -> str:
    with open(pdf_path, "rb") as fh:
        raw = base64.b64encode(fh.read()).decode("ascii")
    return f"data:application/pdf;base64,{raw}"


class MistralOcrProvider(OcrProvider):
    name = "mistral"

    def __init__(self, config: OcrConfig):
        api_key = (config.api_key or os.environ.get("MISTRAL_API_KEY") or "").strip()
        if not api_key:
            raise ValueError("MISTRAL_API_KEY is required for OCR_PROVIDER=mistral")
        self.config = config
        self.api_key = api_key
        self.model = (config.model or os.environ.get("MISTRAL_OCR_MODEL") or DEFAULT_MISTRAL_OCR_MODEL).strip()
        self.api_url = (
            (config.api_url or os.environ.get("MISTRAL_OCR_API_URL") or DEFAULT_MISTRAL_OCR_URL).strip()
        )

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
            "Running Mistral OCR (%s) for pages %s-%s of %s via %s",
            self.model,
            start_idx + 1,
            end_idx,
            Path(pdf_path).name,
            self.api_url,
        )

        total_pages = len(PdfReader(pdf_path).pages)
        end_idx = min(end_idx, total_pages)
        tmp_path: Optional[str] = None
        try:
            if start_idx == 0 and end_idx >= total_pages:
                document_path = pdf_path
            else:
                tmp_path = _extract_pdf_range(pdf_path, start_idx, end_idx)
                document_path = tmp_path

            payload = {
                "model": self.model,
                "document": {
                    "type": "document_url",
                    "document_url": _encode_pdf_data_uri(document_path),
                },
                "include_image_base64": False,
            }
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            timeout = max(120.0, float(self.config.request_timeout_seconds or 300))
            with httpx.Client(timeout=timeout) as client:
                response = client.post(self.api_url, headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()

            raw_pages = body.get("pages") or []
            pages: list[PageDict] = []
            for offset, item in enumerate(raw_pages):
                if not isinstance(item, dict):
                    markdown = str(item or "")
                else:
                    markdown = (
                        item.get("markdown")
                        or item.get("text")
                        or item.get("content")
                        or ""
                    )
                pages.append(
                    {
                        "page_number": start_idx + offset + 1,
                        "original_markdown": markdown or "",
                        "edited_markdown": None,
                        "is_reviewed": False,
                        "reviewer_notes": None,
                    }
                )

            # Pad missing pages if API returns fewer pages than requested range.
            expected = end_idx - start_idx
            while len(pages) < expected:
                n = start_idx + len(pages) + 1
                emit("warning", "Mistral OCR missing page %s; inserting empty placeholder", n)
                pages.append(
                    {
                        "page_number": n,
                        "original_markdown": "",
                        "edited_markdown": None,
                        "is_reviewed": False,
                        "reviewer_notes": None,
                    }
                )
            return pages[:expected]
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Mistral OCR failed at {self.api_url}: {exc}. "
                "Check MISTRAL_API_KEY and network access to api.mistral.ai."
            ) from exc
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
