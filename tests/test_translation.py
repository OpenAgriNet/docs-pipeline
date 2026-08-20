"""Unit tests for translation providers and service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestTranslationService:
    @pytest.mark.unit
    def test_load_translation_config_defaults(self, monkeypatch):
        from pipeline.translation.service import load_translation_config

        monkeypatch.delenv("TRANSLATION_PROVIDER", raising=False)
        monkeypatch.delenv("TRANSLATION_MODEL", raising=False)
        monkeypatch.setenv("TRANSLATION_VLLM_BASE_URL", "http://localhost:8000/v1")

        config = load_translation_config()

        assert config.provider == "gemma_vllm"
        assert config.model == "gemma-4-31b-it"
        assert config.endpoint == "http://localhost:8000/v1"
        assert config.script_gate_enabled is True
        assert config.script_min_chars == 15
        assert config.script_min_ratio == 0.05

    @pytest.mark.unit
    def test_gemma_provider_requires_endpoint(self):
        from pipeline.translation.base import TranslationConfig
        from pipeline.translation.gemma_vllm import GemmaVllmTranslationProvider

        config = TranslationConfig(provider="gemma_vllm", model="gemma-4", endpoint="")
        with pytest.raises(ValueError, match="TRANSLATION_VLLM_BASE_URL"):
            GemmaVllmTranslationProvider(config)

    @pytest.mark.unit
    def test_gemma_provider_translate(self, monkeypatch):
        from pipeline.translation.base import TranslationConfig
        from pipeline.translation.gemma_vllm import GemmaVllmTranslationProvider

        config = TranslationConfig(
            provider="gemma_vllm",
            model="gemma-4",
            endpoint="http://localhost:8000/v1",
        )
        provider = GemmaVllmTranslationProvider(config)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Translated text"}}],
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch("pipeline.translation.gemma_vllm.httpx.Client", return_value=mock_client):
            result = provider.translate("ટેસ્ટ", source_lang="gu", target_language="en")

        assert result == "Translated text"
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args.kwargs
        assert call_kwargs["json"]["model"] == "gemma-4"

    @pytest.mark.unit
    def test_normalize_detected_language_gujarati_script(self):
        from pipeline.translation.service import normalize_detected_language

        assert normalize_detected_language("zl", "ગુજરાતી ટેક્સ્ટ") == "gu"
        assert normalize_detected_language("en", "English only") == "en"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_translate_pages_skips_english(self, monkeypatch):
        from pipeline.translation.base import TranslationConfig
        from pipeline.translation import service as translation_service

        config = TranslationConfig(
            provider="gemma_vllm",
            model="gemma-4",
            endpoint="http://localhost:8000/v1",
            lang_detect_url="http://lang-detect:3001",
        )

        pages = [
            {
                "page_number": 1,
                "original_markdown": "English content that is long enough for detection.",
                "edited_markdown": None,
            },
            {
                "page_number": 2,
                "original_markdown": "ગુજરાતી સામગ્રી જે અનુવાદ માટે લાંબી છે.",
                "edited_markdown": None,
            },
        ]

        monkeypatch.setattr(
            translation_service,
            "detect_page_languages",
            AsyncMock(return_value={0: "en", 1: "gu"}),
        )

        mock_provider = MagicMock()
        mock_provider.translate.return_value = "Gujarati content translated."
        monkeypatch.setattr(
            translation_service,
            "get_translation_provider",
            lambda cfg=None: mock_provider,
        )

        result = await translation_service.translate_pages(pages, config=config)

        assert result[0].get("translated_markdown") is None
        assert result[0]["detected_language"] == "en"
        assert result[1]["translated_markdown"] == "Gujarati content translated."
        assert result[1]["detected_language"] == "gu"
        mock_provider.translate.assert_called_once()


class TestScriptGate:
    """Regex script gate — decides which pages reach the translation model."""

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "text",
        [
            "National Mission on Edible Oils - Oil Palm (NMEO-OP) operational guidelines.",
            "Table 3.1 | Area | Yield | 12,500 | 3.4 | Rs. 29,000 per hectare subsidy.",
            "1. Introduction\n2. Objectives\n3. Pattern of Assistance\n4. Implementation",
        ],
    )
    def test_english_pages_are_skipped(self, text):
        """The exact shape of page that was misdetected as sw/de/hu/fr/ro."""
        from pipeline.translation.script_detect import analyze_script

        result = analyze_script(text)

        assert result.is_non_english is False
        assert result.language == "en"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "text,expected_lang,expected_script",
        [
            ("राष्ट्रीय खाद्य तेल मिशन के अंतर्गत किसानों को सहायता दी जाएगी।", "hi", "Devanagari"),
            ("ખેડૂતોને આ યોજના હેઠળ સહાય આપવામાં આવશે અને લાભ મળશે.", "gu", "Gujarati"),
            ("இந்த திட்டத்தின் கீழ் விவசாயிகளுக்கு உதவி வழங்கப்படும்.", "ta", "Tamil"),
            ("ఈ పథకం కింద రైతులకు సహాయం అందించబడుతుంది.", "te", "Telugu"),
            ("এই প্রকল্পের অধীনে কৃষকদের সহায়তা দেওয়া হবে।", "bn", "Bengali"),
        ],
    )
    def test_indic_pages_are_flagged(self, text, expected_lang, expected_script):
        from pipeline.translation.script_detect import analyze_script

        result = analyze_script(text)

        assert result.is_non_english is True
        assert result.language == expected_lang
        assert result.script == expected_script

    @pytest.mark.unit
    def test_stray_glyph_does_not_trigger_translation(self):
        """A danda or lone character in English text must not cost a Gemma call."""
        from pipeline.translation.script_detect import analyze_script

        text = "Pattern of Assistance under the scheme is Rs. 29,000 per hectare ₹ । क"

        result = analyze_script(text)

        assert result.is_non_english is False
        assert "min_chars" in result.reason

    @pytest.mark.unit
    def test_mostly_english_page_with_hindi_paragraph_is_translated(self):
        from pipeline.translation.script_detect import analyze_script

        text = (
            "Operational guidelines for the scheme. " * 5
            + "योजना के अंतर्गत किसानों को प्रति हेक्टेयर सहायता राशि दी जाएगी और लाभ मिलेगा।"
        )

        result = analyze_script(text)

        assert result.is_non_english is True
        assert result.language == "hi"

    @pytest.mark.unit
    def test_devanagari_is_marked_ambiguous_gujarati_is_not(self):
        from pipeline.translation.script_detect import analyze_script

        hindi = analyze_script("राष्ट्रीय खाद्य तेल मिशन के अंतर्गत किसानों को सहायता दी जाएगी।")
        gujarati = analyze_script("ખેડૂતોને આ યોજના હેઠળ સહાય આપવામાં આવશે અને લાભ મળશે.")

        assert hindi.ambiguous is True
        assert "mr" in hindi.candidates
        assert gujarati.ambiguous is False

    @pytest.mark.unit
    def test_empty_page_is_english(self):
        from pipeline.translation.script_detect import analyze_script

        assert analyze_script("").is_non_english is False
        assert analyze_script("   \n  ").is_non_english is False

    @pytest.mark.unit
    def test_indic_digits_in_english_table_not_classified(self):
        """Localized numerals must not count as script letters (Kanav P2)."""
        from pipeline.translation.script_detect import analyze_script

        # Arabic-Indic digits embedded in English table text
        text = "Table 3.1 | Area | Yield | " + "٠١٢٣٤٥٦٧٨٩" * 2 + " | subsidy norms."
        result = analyze_script(text)
        assert result.is_non_english is False

        # Devanagari digits only — no alphabetic letters
        result_digits = analyze_script("०१२३४५६७८९" * 3)
        assert result_digits.is_non_english is False
        assert "no alphabetic letters" in result_digits.reason

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_latin_french_page_uses_whole_page_detect(self, monkeypatch):
        from pipeline.translation.base import TranslationConfig
        from pipeline.translation import service

        french = (
            "Les bovins laitiers nécessitent une alimentation équilibrée. "
            "Cette section décrit les protocoles de vaccination et les soins vétérinaires."
        )
        pages = [{"page_number": 1, "original_markdown": french}]

        async def fake_whole_page(text, url, client):
            return "fr"

        monkeypatch.setattr(service, "_detect_whole_page_language", fake_whole_page)

        config = TranslationConfig(provider="gemma_vllm", model="gemma-4")
        detected = await service.detect_page_languages(
            pages, "http://lang-detect:3000", config=config
        )
        assert detected == {0: "fr"}

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_stale_translation_cleared_when_page_is_english(self, monkeypatch):
        from unittest.mock import MagicMock

        from pipeline.translation.base import TranslationConfig
        from pipeline.translation import service

        pages = [
            {
                "page_number": 1,
                "original_markdown": "Operational guidelines for oil palm production.",
                "translated_markdown": "Polluted Gemma output from old false positive.",
                "translation_provider": "gemma_vllm",
            }
        ]

        async def fake_latin(pages, indices, detected, non_en, url, log=None):
            for i in indices:
                detected[i] = "en"
                service.clear_machine_translation(pages[i])

        monkeypatch.setattr(service, "_detect_latin_script_pages", fake_latin)

        config = TranslationConfig(provider="gemma_vllm", model="gemma-4")
        mock_provider = MagicMock()
        monkeypatch.setattr(service, "get_translation_provider", lambda cfg=None: mock_provider)

        result = await service.translate_pages(pages, config=config)

        assert result[0]["detected_language"] == "en"
        assert result[0].get("translated_markdown") is None
        assert result[0].get("translation_provider") is None
        mock_provider.translate.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_gate_uses_whole_page_detect_for_latin_english(self, monkeypatch):
        """Latin-script English pages use one whole-page detect call, not per-line batch."""
        from pipeline.translation.base import TranslationConfig
        from pipeline.translation import service

        pages = [
            {"page_number": 1, "original_markdown": "Operational guidelines for oil palm."},
            {"page_number": 2, "original_markdown": "Pattern of assistance and subsidy norms."},
        ]
        calls = {"whole": 0}

        async def fake_whole(text, url, client):
            calls["whole"] += 1
            return "en"

        monkeypatch.setattr(service, "_detect_whole_page_language", fake_whole)

        config = TranslationConfig(provider="gemma_vllm", model="gemma-4")
        detected = await service.detect_page_languages(
            pages, "http://lang-detect:3000", config=config
        )

        assert detected == {0: "en", 1: "en"}
        assert calls["whole"] == 2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_gate_logs_decision_per_page(self, monkeypatch):
        from pipeline.translation.base import TranslationConfig
        from pipeline.translation import service

        async def fake_whole(text, url, client):
            return "en"

        monkeypatch.setattr(service, "_detect_whole_page_language", fake_whole)

        pages = [
            {"page_number": 1, "original_markdown": "Operational guidelines for oil palm."},
            {"page_number": 2, "original_markdown": "ખેડૂતોને આ યોજના હેઠળ સહાય આપવામાં આવશે અને લાભ મળશે."},
        ]
        messages = []

        def log(msg, *args):
            messages.append(msg % args if args else msg)

        config = TranslationConfig(provider="gemma_vllm", model="gemma-4")
        detected = await service.detect_page_languages(
            pages, "http://lang-detect:3000", log=log, config=config
        )

        assert detected == {0: "en", 1: "gu"}
        joined = "\n".join(messages)
        assert "Page 1" in joined and ("SKIP translation" in joined or "whole-page lang-detect" in joined)
        assert "Page 2: regex" in joined and "TRANSLATE" in joined
        assert "1/2 page(s) need translation" in joined
