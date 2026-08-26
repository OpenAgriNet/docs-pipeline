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
                "edited_translation": "Reviewer-approved translation — must survive retry.",
                "translation_provider": "gemma_vllm",
                "translation_model": "gemma-4",
                "translation_target_language": "en",
                "translated_at": "2026-01-01T00:00:00",
            }
        ]

        async def fake_latin(
            pages,
            indices,
            detected,
            non_en,
            url,
            log=None,
            progress_callback=None,
            completed_indices=None,
            pages_total=0,
            detection_completed=0,
        ):
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
        assert result[0].get("translation_model") is None
        assert result[0].get("translation_target_language") is None
        assert result[0].get("translated_at") is None
        # Reviewer field must not be wiped when clearing machine output.
        assert (
            result[0].get("edited_translation")
            == "Reviewer-approved translation — must survive retry."
        )
        mock_provider.translate.assert_not_called()

    @pytest.mark.unit
    def test_clear_machine_translation_preserves_edited_translation(self):
        from pipeline.translation import service

        page = {
            "translated_markdown": "machine junk",
            "edited_translation": "human edit",
            "translation_provider": "gemma_vllm",
            "translation_model": "gemma-4",
            "translation_target_language": "en",
            "translated_at": "2026-01-01T00:00:00",
            "translation_reviewed": 1,
            "translation_notes": "looks good",
        }
        service.clear_machine_translation(page)
        assert page["translated_markdown"] is None
        assert page["translation_provider"] is None
        assert page["edited_translation"] == "human edit"
        assert page["translation_reviewed"] == 1
        assert page["translation_notes"] == "looks good"

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

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_translate_pages_resume_skips_pretranslated_pages(self, monkeypatch):
        from pipeline.translation.base import TranslationConfig
        from pipeline.translation import service as translation_service

        config = TranslationConfig(
            provider="gemma_vllm",
            model="gemma-4",
            endpoint="http://localhost:8000/v1",
            lang_detect_url="http://lang-detect:3000",
        )

        pages = [
            {
                "page_number": 1,
                "original_markdown": "ગુજરાતી પાનું એક",
                "edited_markdown": None,
                "translated_markdown": "already translated",
            },
            {
                "page_number": 2,
                "original_markdown": "ગુજરાતી પાનું બે",
                "edited_markdown": None,
                "translated_markdown": None,
            },
        ]

        monkeypatch.setattr(
            translation_service,
            "detect_page_languages",
            AsyncMock(return_value={0: "gu", 1: "gu"}),
        )

        mock_provider = MagicMock()
        mock_provider.translate.return_value = "fresh translation"
        monkeypatch.setattr(
            translation_service,
            "get_translation_provider",
            lambda cfg=None: mock_provider,
        )

        result = await translation_service.translate_pages(pages, config=config)

        assert result[0]["translated_markdown"] == "already translated"
        assert result[1]["translated_markdown"] == "fresh translation"
        mock_provider.translate.assert_called_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_translate_pages_reports_liveness_while_a_page_is_retrying(self, monkeypatch):
        """A page inside the retry ladder must not look idle to the heartbeat."""
        import asyncio

        from pipeline.translation.base import TranslationConfig
        from pipeline.translation import service as translation_service

        config = TranslationConfig(
            provider="gemma_vllm",
            model="gemma-4",
            endpoint="http://localhost:8000/v1",
            lang_detect_url="http://lang-detect:3000",
            max_retries=3,
            retry_base_seconds=0.5,
        )
        # Shorter than the stall below so liveness events are observable.
        monkeypatch.setattr(translation_service, "PROGRESS_LIVENESS_INTERVAL_SECONDS", 0.02)

        pages = [{"page_number": 1, "original_markdown": "ગુજરાતી", "edited_markdown": None}]

        monkeypatch.setattr(
            translation_service,
            "detect_page_languages",
            AsyncMock(return_value={0: "gu"}),
        )

        attempts = {"count": 0}

        def _flaky_translate(text, source_lang=None, target_language=None):
            attempts["count"] += 1
            if attempts["count"] == 1:
                # First attempt fails with a retryable error, forcing backoff.
                raise RuntimeError("connection reset")
            return "translated at last"

        mock_provider = MagicMock()
        mock_provider.translate.side_effect = _flaky_translate
        monkeypatch.setattr(
            translation_service,
            "get_translation_provider",
            lambda cfg=None: mock_provider,
        )

        events: list[dict] = []

        async def _capture(event):
            events.append(event)
            await asyncio.sleep(0)

        result = await translation_service.translate_pages(
            pages, config=config, progress_callback=_capture
        )

        assert result[0]["translated_markdown"] == "translated at last"
        liveness = [e for e in events if "pages_in_flight" in e]
        assert liveness, "expected at least one liveness event during retry backoff"
        assert all(e["pages_in_flight"] == 1 for e in liveness)
        assert all("translated_page" not in e for e in liveness)
        # The completion event still arrives exactly once, with the page attached.
        completions = [e for e in events if "translated_page" in e]
        assert len(completions) == 1
        assert completions[0]["pages_completed"] == 1

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_translate_pages_force_retranslate_overwrites_pretranslated(self, monkeypatch):
        from pipeline.translation.base import TranslationConfig
        from pipeline.translation import service as translation_service

        config = TranslationConfig(
            provider="gemma_vllm",
            model="gemma-4",
            endpoint="http://localhost:8000/v1",
            lang_detect_url="http://lang-detect:3000",
        )

        pages = [
            {
                "page_number": 1,
                "original_markdown": "ગુજરાતી પાનું એક",
                "edited_markdown": None,
                "translated_markdown": "stale translation",
                "edited_translation": "human edit",
            },
            {
                "page_number": 2,
                "original_markdown": "ગુજરાતી પાનું બે",
                "edited_markdown": None,
                "translated_markdown": "stale translation 2",
            },
        ]

        monkeypatch.setattr(
            translation_service,
            "detect_page_languages",
            AsyncMock(return_value={0: "gu", 1: "gu"}),
        )

        mock_provider = MagicMock()
        mock_provider.translate.side_effect = ["new one", "new two"]
        monkeypatch.setattr(
            translation_service,
            "get_translation_provider",
            lambda cfg=None: mock_provider,
        )

        result = await translation_service.translate_pages(
            pages,
            config=config,
            force_retranslate=True,
        )

        assert result[0]["translated_markdown"] == "new one"
        assert result[1]["translated_markdown"] == "new two"
        assert result[0]["edited_translation"] == "human edit"
        assert mock_provider.translate.call_count == 2

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_detect_page_languages_reports_progress(self, monkeypatch):
        from pipeline.translation import service as translation_service

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"results": [{"language": "en"}]}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, json):
                return _Resp()

        monkeypatch.setattr(translation_service.httpx, "AsyncClient", lambda timeout=60.0: _Client())

        events = []
        pages = [
            {"page_number": 1, "original_markdown": "This is a long enough english line for detection."},
            {"page_number": 2, "original_markdown": "Another sufficiently long english line for detection."},
        ]
        detected = await translation_service.detect_page_languages(
            pages,
            "http://lang-detect:3000",
            progress_callback=lambda event: events.append(event),
        )

        assert detected == {0: "en", 1: "en"}
        assert len(events) == 2
        assert all(event.get("phase") == "detection" for event in events)
        assert events[-1]["pages_completed"] == 2
