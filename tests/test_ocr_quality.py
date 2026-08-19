"""Tests for OCR quality heuristics."""

import pytest

from pipeline.ocr.quality import degenerate_ocr_note, is_degenerate_repetition


class TestOcrQuality:
    @pytest.mark.unit
    def test_detects_repetition_loop(self):
        text = "o" * 600
        assert is_degenerate_repetition(text) is True
        note = degenerate_ocr_note(text)
        assert note is not None
        assert "Degenerate OCR" in note

    @pytest.mark.unit
    def test_ignores_short_or_normal_text(self):
        assert is_degenerate_repetition("hello world") is False
        assert degenerate_ocr_note("hello world") is None
