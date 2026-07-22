"""Translation service layer, language detection, and provider selection."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import Callable, Optional

import httpx

from .base import TranslationConfig, TranslationProvider
from .gemma_vllm import GemmaVllmTranslationProvider

PROVIDERS: dict[str, type[TranslationProvider]] = {
    "gemma_vllm": GemmaVllmTranslationProvider,
    "gemma4": GemmaVllmTranslationProvider,
    "gemma": GemmaVllmTranslationProvider,
}

LANG_MAP = {
    "english": "en",
    "hindi": "hi",
    "gujarati": "gu",
    "marathi": "mr",
    "tamil": "ta",
    "telugu": "te",
    "kannada": "kn",
    "malayalam": "ml",
    "punjabi": "pa",
    "bengali": "bn",
    "oriya": "or",
    "odia": "or",
    # Observed noisy code from lang-detect service for Gujarati pages.
    "zl": "gu",
}


def _gemma_endpoint() -> str:
    """Resolve OpenAI-compatible Gemma base URL (AGRINET preferred)."""
    return (
        os.environ.get("AGRINET_GEMMA_BASE_URL")
        or os.environ.get("TRANSLATION_VLLM_BASE_URL")
        or "http://localhost:8020/v1"
    ).strip()


def _gemma_model() -> str:
    return (
        os.environ.get("AGRINET_GEMMA_MODEL_NAME")
        or os.environ.get("TRANSLATION_MODEL")
        or "google/gemma-4-31b-it"
    ).strip() or "google/gemma-4-31b-it"


def load_translation_config(target_language: str = "en") -> TranslationConfig:
    provider = os.environ.get("TRANSLATION_PROVIDER", "gemma_vllm").strip().lower()
    model = _gemma_model()
    endpoint = _gemma_endpoint()
    # Ensure OpenAI-compatible path: base may be .../gemma4 without trailing /v1
    if endpoint and not endpoint.rstrip("/").endswith("/v1"):
        endpoint = endpoint.rstrip("/") + "/v1"
    api_key = (
        os.environ.get("TRANSLATION_API_KEY")
        or os.environ.get("AGRINET_GEMMA_API_KEY")
        or ""
    ).strip()
    return TranslationConfig(
        provider=provider,
        model=model,
        endpoint=endpoint,
        api_key=api_key,
        target_language=target_language,
        page_concurrency=max(1, int(os.environ.get("TRANSLATION_PAGE_CONCURRENCY", "1"))),
        max_retries=max(1, int(os.environ.get("TRANSLATION_MAX_RETRIES", "6"))),
        retry_base_seconds=max(0.5, float(os.environ.get("TRANSLATION_RETRY_BASE_SECONDS", "2.0"))),
        max_output_tokens=int(os.environ.get("TRANSLATION_MAX_OUTPUT_TOKENS", "8000")),
        request_timeout_seconds=float(os.environ.get("TRANSLATION_REQUEST_TIMEOUT_SECONDS", "300")),
        lang_detect_url=os.environ.get("LANG_DETECT_URL", "http://localhost:3001"),
    )


def get_translation_provider(config: Optional[TranslationConfig] = None) -> TranslationProvider:
    config = config or load_translation_config()
    provider_cls = PROVIDERS.get(config.provider)
    if provider_cls is None:
        supported = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"Unsupported translation provider '{config.provider}'. Supported: {supported}")
    return provider_cls(config)


def clean_translation(text: str) -> str:
    result = text
    result = re.sub(
        r"^Here is the translated text from \*\*[^*]+\*\* to English[^:]*:?\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the translated text from [^:]+?:\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the translated text[^:]*:?\s*\n*-{0,3}\s*\n*",
        "",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        r"^Here is the (?:English )?translation[^:]*:?\s*\n*",
        "",
        result,
        flags=re.IGNORECASE | re.MULTILINE,
    )

    prefixes = [
        r"^(?:the\s+)?english\s+translation:?\s*\n*",
        r"^(?:the\s+)?translation:?\s*\n*",
        r"^translated\s+(?:text|content):?\s*\n*",
        r"^##?\s*(?:english\s+)?translation\s*\n+",
        r"^---+\s*\n+",
        r"^\*\*Translation:?\*\*\s*\n*",
    ]
    for pattern in prefixes:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE | re.MULTILINE)

    result = re.sub(r"\n*-{3,}\s*$", "", result)
    return result.strip()


def _contains_gujarati_script(text: str) -> bool:
    if not text:
        return False
    return any("\u0A80" <= ch <= "\u0AFF" for ch in text)


def normalize_detected_language(detected_lang: str | None, page_text: str) -> str:
    lowered = (detected_lang or "en").lower()
    normalized = LANG_MAP.get(lowered, lowered[:2] if lowered else "en")
    if normalized in {"unknown", "un", "und", "xx", "zl"}:
        if _contains_gujarati_script(page_text):
            return "gu"
        return "en"
    return normalized


async def detect_page_languages(
    pages: list[dict],
    lang_detect_url: str,
    log: Optional[Callable[..., None]] = None,
) -> dict[int, str]:
    detected_languages: dict[int, str] = {}

    async with httpx.AsyncClient(timeout=60.0) as http_client:
        for i, page in enumerate(pages):
            text = page.get("edited_markdown") or page.get("original_markdown", "")
            if not text or len(text.strip()) < 20:
                detected_languages[i] = "en"
                continue

            lines = [line.strip() for line in text.split("\n") if len(line.strip()) >= 10]
            if not lines:
                detected_languages[i] = "en"
                continue

            try:
                response = await http_client.post(
                    f"{lang_detect_url.rstrip('/')}/detect/batch",
                    json={"texts": lines},
                )
                response.raise_for_status()
                results = response.json().get("results", [])

                non_english_lang = None
                for result in results:
                    lang = result.get("language", "en").lower()
                    if lang not in {"en", "unknown"}:
                        non_english_lang = lang
                        if log:
                            log(
                                "Page %s: Found non-English content, detected language: %s",
                                page.get("page_number"),
                                lang,
                            )
                        break

                page_text = page.get("edited_markdown") or page.get("original_markdown", "")
                detected_languages[i] = normalize_detected_language(
                    non_english_lang if non_english_lang else "en",
                    page_text,
                )
            except Exception as exc:
                if log:
                    log("Lang-detect error for page %s: %s: %s", i, type(exc).__name__, exc)
                detected_languages[i] = "en"

    return detected_languages


async def translate_pages(
    pages: list[dict],
    *,
    target_language: str = "en",
    config: Optional[TranslationConfig] = None,
    log: Optional[Callable[..., None]] = None,
) -> list[dict]:
    config = config or load_translation_config(target_language=target_language)
    provider = get_translation_provider(config)

    if log:
        log("Processing %s pages for translation", len(pages))
        log("Using lang-detect service at %s", config.lang_detect_url)
        log("Using translation provider=%s model=%s", config.provider, config.model)
        log(
            "Translation runtime config: concurrency=%s max_retries=%s retry_base_seconds=%s target_language=%s",
            config.page_concurrency,
            config.max_retries,
            config.retry_base_seconds,
            config.target_language,
        )

    detected_languages = await detect_page_languages(pages, config.lang_detect_url, log=log)

    pages_to_translate: list[tuple[int, dict, str]] = []
    for i, page in enumerate(pages):
        if i not in detected_languages:
            continue
        detected_lang = detected_languages[i]
        page["detected_language"] = detected_lang
        if detected_lang != "en":
            pages_to_translate.append((i, page, detected_lang))

    if log:
        log("Found %s pages needing translation", len(pages_to_translate))

    semaphore = asyncio.Semaphore(config.page_concurrency)

    async def translate_page(idx: int, page: dict, lang: str) -> tuple[int, str | None, str | None]:
        async with semaphore:
            text = page.get("edited_markdown") or page.get("original_markdown", "")
            if log:
                log("Translating page %s from %s", page.get("page_number"), lang)
            for attempt in range(1, config.max_retries + 1):
                try:
                    raw_translation = await asyncio.to_thread(
                        provider.translate,
                        text,
                        source_lang=lang,
                        target_language=config.target_language,
                    )
                    return (idx, clean_translation(raw_translation), None)
                except Exception as exc:
                    error_text = str(exc)
                    is_rate_limited = "429" in error_text or "rate limit" in error_text.lower()
                    is_retryable = is_rate_limited or any(
                        token in error_text.lower()
                        for token in ("timeout", "timed out", "connection", "503", "502", "504", "unavailable")
                    )
                    if is_retryable and attempt < config.max_retries:
                        backoff_seconds = config.retry_base_seconds * (2 ** (attempt - 1))
                        if log:
                            log(
                                "Translation error for page %s on attempt %s/%s (%s), retrying in %.1fs",
                                page.get("page_number"),
                                attempt,
                                config.max_retries,
                                error_text,
                                backoff_seconds,
                            )
                        await asyncio.sleep(backoff_seconds)
                        continue
                    if log:
                        log("Translation error for page %s: %s", page.get("page_number"), exc)
                    return (idx, None, error_text)
            return (idx, None, "translation failed")

    results = (
        await asyncio.gather(*[translate_page(i, p, lang) for i, p, lang in pages_to_translate])
        if pages_to_translate
        else []
    )

    translated_count = 0
    translated_at = datetime.utcnow().isoformat()
    failures: list[str] = []
    for idx, translation, error in results:
        if translation:
            pages[idx]["translated_markdown"] = translation
            pages[idx]["translation_provider"] = config.provider
            pages[idx]["translation_model"] = config.model
            pages[idx]["translation_target_language"] = config.target_language
            pages[idx]["translated_at"] = translated_at
            translated_count += 1
        elif error:
            page_no = pages[idx].get("page_number", idx)
            failures.append(f"page {page_no}: {error}")

    if failures:
        raise RuntimeError(
            f"Translation failed for {len(failures)}/{len(pages_to_translate)} page(s). "
            f"First error: {failures[0]}"
        )

    if log:
        log(
            "Translation complete: %s/%s pages translated",
            translated_count,
            len(pages_to_translate),
        )
    return pages
