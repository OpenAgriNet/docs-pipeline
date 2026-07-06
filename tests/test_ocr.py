"""Unit tests for OCR providers."""

from unittest.mock import MagicMock, patch

import pytest


class TestOcrService:
    @pytest.mark.unit
    def test_ocr_pdf_uses_provider(self, monkeypatch, temp_pdf_file):
        from pipeline.ocr import service as ocr_service

        monkeypatch.setenv("OCR_PROVIDER", "chandra")

        mock_provider = MagicMock()
        mock_provider.process_pdf_range.return_value = [
            {
                "page_number": 1,
                "original_markdown": "hello",
                "edited_markdown": None,
                "is_reviewed": False,
                "reviewer_notes": None,
            }
        ]
        monkeypatch.setattr(ocr_service, "get_ocr_provider", lambda config=None: mock_provider)

        pages = ocr_service.ocr_pdf(str(temp_pdf_file), clean_text=lambda text: text.strip())

        assert len(pages) == 1
        assert pages[0]["original_markdown"] == "hello"
        mock_provider.process_pdf_range.assert_called_once()
