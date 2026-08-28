"""Translation service layer, language detection, and provider selection."""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import Awaitable, Callable, Optional

import httpx

from .base import TranslationConfig, TranslationProvider
from .gemma_vllm import GemmaVllmTranslationProvider
from .script_detect import analyze_script, script_family

PROVIDERS: dict[str, type[TranslationProvider]] = {
    "gemma_vllm": GemmaVllmTranslationProvider,
    "gemma4": GemmaVllmTranslationProvider,
    "gemma": GemmaVllmTranslationProvider,
}

# A page can stay in flight for the whole provider retry ladder (max_retries
# requests at request_timeout_seconds each, plus backoff), which is far longer
# than the activity heartbeat timeout. Report liveness on this interval so a
# still-working translation is not killed for looking idle.
PROGRESS_LIVENESS_INTERVAL_SECONDS = 60.0

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


def load_translation_config(target_language: str = "en") -> TranslationConfig:
    provider = os.environ.get("TRANSLATION_PROVIDER", "gemma_vllm").strip().lower()
    default_model = "gemma-4-31b-it"
    model = os.environ.get("TRANSLATION_MODEL", default_model).strip() or default_model
    endpoint = os.environ.get("TRANSLATION_VLLM_BASE_URL", "http://localhost:8020/v1").strip()
    api_key = os.environ.get("TRANSLATION_API_KEY", "").strip()
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
        lang_detect_url=os.environ.get("LANG_DETECT_URL", "http://lang-detect:3000"),
        script_gate_enabled=(
            os.environ.get("TRANSLATION_SCRIPT_GATE_ENABLED", "true").strip().lower()
            not in {"false", "0", "no"}
        ),
        script_min_chars=max(1, int(os.environ.get("TRANSLATION_SCRIPT_MIN_CHARS", "15"))),
        script_min_ratio=max(0.0, float(os.environ.get("TRANSLATION_SCRIPT_MIN_RATIO", "0.05"))),
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


def clear_machine_translation(page: dict) -> None:
    """Drop stale Gemma output when a page is reclassified as English.

    Clears only machine fields (``translated_markdown`` + provenance).
    ``edited_translation`` is reviewer-edited and audit-tracked — leave it alone
    so a translation retry cannot erase human work.
    """
    page["translated_markdown"] = None
    page["translation_provider"] = None
    page["translation_model"] = None
    page["translation_target_language"] = None
    page["translated_at"] = None


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
    progress_callback: Optional[Callable[[dict], None | Awaitable[None]]] = None,
    config: Optional[TranslationConfig] = None,
) -> dict[int, str]:
    """Decide each page's language, gating on script regex before any model call."""
    config = config or load_translation_config()

    if config.script_gate_enabled:
        return await _detect_via_script_gate(
            pages,
            lang_detect_url,
            config,
            log=log,
            progress_callback=progress_callback,
        )

    if log:
        log("Script gate DISABLED — falling back to per-line lang-detect for all pages")
    return await _detect_via_lang_detect(
        pages,
        lang_detect_url,
        log=log,
        progress_callback=progress_callback,
    )


async def _detect_via_script_gate(
    pages: list[dict],
    lang_detect_url: str,
    config: TranslationConfig,
    log: Optional[Callable[..., None]] = None,
    progress_callback: Optional[Callable[[dict], None | Awaitable[None]]] = None,
) -> dict[int, str]:
    detected_languages: dict[int, str] = {}
    non_english: list[int] = []
    ambiguous: list[int] = []
    latin_script_indices: list[int] = []
    pages_total = len(pages)
    detection_completed = 0
    completed_indices: set[int] = set()

    async def _emit_detection_progress(index: int, page_number: int | None) -> None:
        nonlocal detection_completed
        if index in completed_indices:
            return
        completed_indices.add(index)
        detection_completed += 1
        if not progress_callback:
            return
        maybe_awaitable = progress_callback(
            {
                "phase": "detection",
                "pages_total": pages_total,
                "pages_completed": detection_completed,
                "page_number": page_number,
            }
        )
        if asyncio.iscoroutine(maybe_awaitable):
            await maybe_awaitable

    if log:
        log(
            "Script gate: scanning %s page(s) with regex (min_chars=%s min_ratio=%s)",
            len(pages),
            config.script_min_chars,
            config.script_min_ratio,
        )

    for i, page in enumerate(pages):
        text = page.get("edited_markdown") or page.get("original_markdown", "")
        page_no = page.get("page_number", i + 1)
        analysis = analyze_script(
            text,
            min_chars=config.script_min_chars,
            min_ratio=config.script_min_ratio,
        )

        if not analysis.is_non_english:
            if analysis.needs_latin_lang_detect:
                latin_script_indices.append(i)
                if log:
                    log(
                        "Page %s: regex → %s → whole-page lang-detect",
                        page_no,
                        analysis.summary(),
                    )
            else:
                detected_languages[i] = "en"
                clear_machine_translation(page)
                await _emit_detection_progress(i, page_no)
                if log:
                    log("Page %s: regex → %s → SKIP translation", page_no, analysis.summary())
            continue

        detected_languages[i] = analysis.language
        non_english.append(page_no)
        if log:
            log("Page %s: regex → %s → TRANSLATE", page_no, analysis.summary())
        if analysis.ambiguous:
            ambiguous.append(i)
            continue
        await _emit_detection_progress(i, page_no)

    if latin_script_indices:
        await _detect_latin_script_pages(
            pages,
            latin_script_indices,
            detected_languages,
            non_english,
            lang_detect_url,
            log=log,
            progress_callback=progress_callback,
            completed_indices=completed_indices,
            pages_total=pages_total,
            detection_completed=detection_completed,
        )

    if ambiguous:
        await _disambiguate_languages(
            pages,
            ambiguous,
            detected_languages,
            lang_detect_url,
            log=log,
            on_page_done=_emit_detection_progress,
        )

    if log:
        log(
            "Script gate result: %s/%s page(s) need translation %s; %s skipped as English",
            len(non_english),
            len(pages),
            non_english or "[]",
            len(pages) - len(non_english),
        )

    return detected_languages


async def _detect_whole_page_language(
    text: str,
    lang_detect_url: str,
    http_client: httpx.AsyncClient,
) -> str:
    """Detect language from full page text (Latin-script pages only)."""
    sample = text.strip()
    if len(sample) < 20:
        return "en"
    if len(sample) > 8000:
        sample = sample[:8000]
    response = await http_client.post(
        f"{lang_detect_url.rstrip('/')}/detect",
        json={"text": sample},
    )
    response.raise_for_status()
    raw = str(response.json().get("language", "en")).lower()
    return normalize_detected_language(raw, text)


async def _detect_latin_script_pages(
    pages: list[dict],
    indices: list[int],
    detected_languages: dict[int, str],
    non_english: list[int],
    lang_detect_url: str,
    log: Optional[Callable[..., None]] = None,
    progress_callback: Optional[Callable[[dict], None | Awaitable[None]]] = None,
    completed_indices: Optional[set[int]] = None,
    pages_total: int = 0,
    detection_completed: int = 0,
) -> None:
    """Run whole-page lang-detect for Latin-script text (French, Spanish, etc.)."""
    if log:
        log("Script gate: %s Latin-script page(s) → whole-page lang-detect", len(indices))

    async with httpx.AsyncClient(timeout=60.0) as http_client:
        for i in indices:
            page = pages[i]
            page_no = page.get("page_number", i + 1)
            text = page.get("edited_markdown") or page.get("original_markdown", "")
            try:
                lang = await _detect_whole_page_language(text, lang_detect_url, http_client)
            except Exception as exc:
                if log:
                    log(
                        "Page %s: whole-page lang-detect failed (%s: %s), defaulting to en",
                        page_no,
                        type(exc).__name__,
                        exc,
                    )
                lang = "en"

            detected_languages[i] = lang
            if lang != "en":
                non_english.append(page_no)
                if log:
                    log("Page %s: whole-page lang-detect → %s → TRANSLATE", page_no, lang)
            else:
                clear_machine_translation(page)
                if log:
                    log("Page %s: whole-page lang-detect → en → SKIP translation", page_no)
            if progress_callback:
                done = completed_indices if completed_indices is not None else set()
                if i not in done:
                    done.add(i)
                    completed = max(detection_completed, len(done))
                    maybe_awaitable = progress_callback(
                        {
                            "phase": "detection",
                            "pages_total": pages_total or len(pages),
                            "pages_completed": completed,
                            "page_number": page_no,
                        }
                    )
                    if asyncio.iscoroutine(maybe_awaitable):
                        await maybe_awaitable


async def _disambiguate_languages(
    pages: list[dict],
    indices: list[int],
    detected_languages: dict[int, str],
    lang_detect_url: str,
    log: Optional[Callable[..., None]] = None,
    on_page_done: Optional[Callable[[int, int | None], Awaitable[None]]] = None,
) -> None:
    """Refine shared-script pages (e.g. Devanagari → hi vs mr) via lang-detect."""
    if log:
        log(
            "Script gate: %s page(s) on a shared script, asking lang-detect to disambiguate",
            len(indices),
        )

    async with httpx.AsyncClient(timeout=60.0) as http_client:
        for i in indices:
            page = pages[i]
            page_no = page.get("page_number", i + 1)
            try:
                default_lang = detected_languages[i]
                family = script_family(default_lang)
                text = page.get("edited_markdown") or page.get("original_markdown", "")
                lines = [line.strip() for line in text.split("\n") if len(line.strip()) >= 10]
                if not lines:
                    continue

                try:
                    response = await http_client.post(
                        f"{lang_detect_url.rstrip('/')}/detect/batch",
                        json={"texts": lines},
                    )
                    response.raise_for_status()
                    results = response.json().get("results", [])
                except Exception as exc:
                    if log:
                        log(
                            "Page %s: lang-detect unavailable (%s: %s), keeping regex default %s",
                            page_no,
                            type(exc).__name__,
                            exc,
                            default_lang,
                        )
                    continue

                votes: dict[str, int] = {}
                for result in results:
                    raw = str(result.get("language", "")).lower()
                    candidate = LANG_MAP.get(raw, raw[:2] if raw else "")
                    if candidate in family:
                        votes[candidate] = votes.get(candidate, 0) + 1

                if not votes:
                    if log:
                        log(
                            "Page %s: lang-detect returned nothing in the %s family, keeping %s",
                            page_no,
                            "/".join(family),
                            default_lang,
                        )
                    continue

                winner = max(votes, key=lambda k: votes[k])
                if winner != default_lang:
                    detected_languages[i] = winner
                    if log:
                        log(
                            "Page %s: lang-detect refined %s → %s (votes=%s)",
                            page_no,
                            default_lang,
                            winner,
                            votes,
                        )
            finally:
                if on_page_done:
                    await on_page_done(i, page_no)


async def _detect_via_lang_detect(
    pages: list[dict],
    lang_detect_url: str,
    log: Optional[Callable[..., None]] = None,
    progress_callback: Optional[Callable[[dict], None | Awaitable[None]]] = None,
) -> dict[int, str]:
    """Legacy per-line detection, kept for TRANSLATION_SCRIPT_GATE_ENABLED=false."""
    detected_languages: dict[int, str] = {}
    pages_total = len(pages)
    detection_completed = 0

    async def _emit_detection_progress(page_number: int | None) -> None:
        nonlocal detection_completed
        detection_completed += 1
        if not progress_callback:
            return
        maybe_awaitable = progress_callback(
            {
                "phase": "detection",
                "pages_total": pages_total,
                "pages_completed": detection_completed,
                "page_number": page_number,
            }
        )
        if asyncio.iscoroutine(maybe_awaitable):
            await maybe_awaitable

    async with httpx.AsyncClient(timeout=60.0) as http_client:
        for i, page in enumerate(pages):
            text = page.get("edited_markdown") or page.get("original_markdown", "")
            if not text or len(text.strip()) < 20:
                detected_languages[i] = "en"
                await _emit_detection_progress(page.get("page_number"))
                continue

            lines = [line.strip() for line in text.split("\n") if len(line.strip()) >= 10]
            if not lines:
                detected_languages[i] = "en"
                await _emit_detection_progress(page.get("page_number"))
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
            await _emit_detection_progress(page.get("page_number"))

    return detected_languages


async def translate_pages(
    pages: list[dict],
    *,
    target_language: str = "en",
    config: Optional[TranslationConfig] = None,
    log: Optional[Callable[..., None]] = None,
    force_retranslate: bool = False,
    progress_callback: Optional[Callable[[dict], None | Awaitable[None]]] = None,
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

    detected_languages = await detect_page_languages(
        pages,
        config.lang_detect_url,
        log=log,
        progress_callback=progress_callback,
        config=config,
    )

    pages_to_translate: list[tuple[int, dict, str]] = []
    skipped_existing = 0
    for i, page in enumerate(pages):
        if i not in detected_languages:
            continue
        detected_lang = detected_languages[i]
        page["detected_language"] = detected_lang
        if detected_lang == "en":
            clear_machine_translation(page)
        elif detected_lang != "en":
            if page.get("translated_markdown") and not force_retranslate:
                skipped_existing += 1
                continue
            pages_to_translate.append((i, page, detected_lang))

    if log:
        log(
            "Found %s pages needing translation (skipped_existing=%s, force_retranslate=%s)",
            len(pages_to_translate),
            skipped_existing,
            force_retranslate,
        )

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

    translated_count = 0
    translated_at = datetime.utcnow().isoformat()
    failures: list[str] = []
    completed_count = 0
    failed_count = 0
    async def emit_progress(event: dict) -> None:
        if not progress_callback:
            return
        maybe_awaitable = progress_callback(event)
        if asyncio.iscoroutine(maybe_awaitable):
            await maybe_awaitable

    tasks = [asyncio.create_task(translate_page(i, p, lang)) for i, p, lang in pages_to_translate]
    pending = set(tasks)
    while pending:
        done, pending = await asyncio.wait(
            pending,
            timeout=PROGRESS_LIVENESS_INTERVAL_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            await emit_progress(
                {
                    "phase": "translation",
                    "pages_total": len(pages_to_translate),
                    "pages_completed": completed_count,
                    "translated_count": translated_count,
                    "failed_count": failed_count,
                    "pages_in_flight": len(pending),
                }
            )
            continue
        for task in done:
            idx, translation, error = await task
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
                failed_count += 1
            completed_count += 1
            event = {
                "phase": "translation",
                "pages_total": len(pages_to_translate),
                "pages_completed": completed_count,
                "translated_count": translated_count,
                "failed_count": failed_count,
                "last_page_number": pages[idx].get("page_number"),
            }
            if translation:
                event["translated_page"] = pages[idx]
            await emit_progress(event)

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
