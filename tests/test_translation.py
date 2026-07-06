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
        assert config.model == "gemma-4"
        assert config.endpoint == "http://localhost:8000/v1"

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
