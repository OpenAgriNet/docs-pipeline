"""OCR providers and service layer."""

from .base import OcrConfig, OcrProvider
from .chandra_vllm import ChandraVllmOcrProvider
from .service import get_ocr_provider, load_ocr_config, ocr_pdf, ocr_pdf_in_segments

__all__ = [
    "ChandraVllmOcrProvider",
    "OcrConfig",
    "OcrProvider",
    "get_ocr_provider",
    "load_ocr_config",
    "ocr_pdf",
    "ocr_pdf_in_segments",
]