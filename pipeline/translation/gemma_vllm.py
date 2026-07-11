"""Gemma translation via an OpenAI-compatible vLLM endpoint."""

from __future__ import annotations

import httpx

from .base import TranslationConfig, TranslationProvider

TRANSLATE_PROMPT = """Translate the following text to English.
Preserve all formatting, including markdown syntax, tables, and bullet points.
Maintain technical terminology accurately.
If there are proper nouns or names, keep them as-is or transliterate appropriately.
Do not include any preamble or introduction — start directly with the translated content.

Original text:
{text}"""


class GemmaVllmTranslationProvider(TranslationProvider):
    name = "gemma_vllm"

    def __init__(self, config: TranslationConfig):
        if not config.endpoint:
            raise ValueError("TRANSLATION_VLLM_BASE_URL is required for gemma translation")
        self.config = config
        self._endpoint = config.endpoint.rstrip("/") + "/chat/completions"

    def translate(self, text: str, *, source_lang: str, target_language: str) -> str:
        del source_lang, target_language
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": TRANSLATE_PROMPT.format(text=text)}],
            "temperature": 0.0,
            "max_tokens": self.config.max_output_tokens,
        }

        with httpx.Client(timeout=self.config.request_timeout_seconds) as client:
            response = client.post(self._endpoint, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        choices = data.get("choices") or []
        if not choices:
            raise ValueError("Gemma translation response contained no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not content:
            raise ValueError("Gemma translation response contained empty content")
        return content
