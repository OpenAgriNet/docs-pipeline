"""Unit tests for OCR providers."""

import sys
import types
from unittest.mock import MagicMock

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


class TestChandraVllmPageSlice:
    @pytest.mark.unit
    def test_vllm_uses_same_half_open_fitz_slice_as_hf(self, monkeypatch):
        """vLLM must OCR pages [start, end), not Chandra load_file's off-by-one range."""
        from pipeline.ocr.base import OcrConfig
        from pipeline.ocr.chandra_vllm import ChandraVllmOcrProvider, OCR_LAYOUT_PROMPT_MARKER

        fake_images = [object(), object()]
        captured: dict = {}

        class FakeItem:
            def __init__(self, image, prompt_type):
                self.image = image
                self.prompt_type = prompt_type

        class FakeResult:
            def __init__(self, markdown):
                self.markdown = markdown
                self.error = False

        class FakeManager:
            def __init__(self, method):
                captured["method"] = method

            def generate(self, batch, **kwargs):
                captured["batch"] = batch
                captured["kwargs"] = kwargs
                return [FakeResult(f"md-{idx}") for idx in range(len(batch))]

        def boom_load_file(*_args, **_kwargs):
            raise AssertionError("vLLM OCR must not use chandra.input.load_file page_range")

        fake_schema = types.ModuleType("chandra.model.schema")
        fake_schema.BatchInputItem = FakeItem
        fake_model = types.ModuleType("chandra.model")
        fake_model.InferenceManager = FakeManager
        fake_input = types.ModuleType("chandra.input")
        fake_input.load_file = boom_load_file
        fake_chandra = types.ModuleType("chandra")
        monkeypatch.setitem(sys.modules, "chandra", fake_chandra)
        monkeypatch.setitem(sys.modules, "chandra.model", fake_model)
        monkeypatch.setitem(sys.modules, "chandra.model.schema", fake_schema)
        monkeypatch.setitem(sys.modules, "chandra.input", fake_input)

        raster = MagicMock(return_value=fake_images)
        monkeypatch.setattr("pipeline.ocr.chandra_vllm._pdf_pages_as_images", raster)

        provider = ChandraVllmOcrProvider(
            OcrConfig(
                provider="chandra",
                model="chandra",
                endpoint="http://host.docker.internal:8011/v1",
                inference_mode="vllm",
                max_output_tokens=12288,
                max_workers=4,
                image_dpi=192,
            )
        )
        pages = provider.process_pdf_range("doc.pdf", 0, 2)

        raster.assert_called_once_with("doc.pdf", 0, 2, dpi=192)
        assert captured["method"] == "vllm"
        assert [item.image for item in captured["batch"]] == fake_images
        assert all(item.prompt_type == OCR_LAYOUT_PROMPT_MARKER for item in captured["batch"])
        assert [page["page_number"] for page in pages] == [1, 2]
        assert [page["original_markdown"] for page in pages] == ["md-0", "md-1"]
        assert captured["kwargs"]["vllm_api_base"] == "http://host.docker.internal:8011/v1"
