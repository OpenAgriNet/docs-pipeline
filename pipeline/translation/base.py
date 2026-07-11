"""Translation provider interfaces and shared structures."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class TranslationConfig:
    provider: str
    model: str
    endpoint: str = ""
    api_key: str = ""
    target_language: str = "en"
    page_concurrency: int = 1
    max_retries: int = 6
    retry_base_seconds: float = 2.0
    max_output_tokens: int = 8000
    request_timeout_seconds: float = 300.0
    lang_detect_url: str = "http://localhost:3001"


class TranslationProvider(ABC):
    name: str

    @abstractmethod
    def translate(self, text: str, *, source_lang: str, target_language: str) -> str:
        """Translate text to the target language."""
        raise NotImplementedError
