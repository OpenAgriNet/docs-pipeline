"""Translation providers, the script gate, and the page translation service."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pipeline.translation import service
from pipeline.translation.base import TranslationConfig
from pipeline.translation.gemma_vllm import GemmaVllmTranslationProvider
from pipeline.translation.script_detect import analyze_script

DETECT_URL = "http://lang-detect:3000"
CONFIG = TranslationConfig(
    provider="gemma_vllm", model="gemma-4", endpoint="http://localhost:8000/v1", lang_detect_url=DETECT_URL
)
GATE_CONFIG = TranslationConfig(provider="gemma_vllm", model="gemma-4")
HINDI = "राष्ट्रीय खाद्य तेल मिशन के अंतर्गत किसानों को सहायता दी जाएगी।"
GUJARATI = "ખેડૂતોને આ યોજના હેઠળ સહાય આપવામાં આવશે અને લાભ મળશે."


def _page(n, text, **fields):
    return {"page_number": n, "original_markdown": text, "edited_markdown": None, **fields}


def _texts(*texts):
    return [{"page_number": n, "original_markdown": t} for n, t in enumerate(texts, start=1)]


def _translator(monkeypatch, languages, reply):
    """Detection answers ``languages`` (one per page); returns the provider mock answering ``reply``.

    ``reply`` is one translation for every call, or a list / callable used as the side effect.
    """
    monkeypatch.setattr(service, "detect_page_languages", AsyncMock(return_value=dict(enumerate(languages))))
    provider = MagicMock()
    if isinstance(reply, str):
        provider.translate.return_value = reply
    else:
        provider.translate.side_effect = reply
    monkeypatch.setattr(service, "get_translation_provider", lambda cfg=None: provider)
    return provider


def _whole_page_detect(monkeypatch, language):
    """Stub the whole-page lang-detect call; returns the texts it was asked about."""
    asked = []

    async def detect(text, url, client):
        asked.append(text)
        return language

    monkeypatch.setattr(service, "_detect_whole_page_language", detect)
    return asked


def _lang_detect_http(monkeypatch, results, before_reply=None):
    """Answer every lang-detect HTTP POST with ``results``, awaiting ``before_reply()`` first."""

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            if before_reply:
                await before_reply()
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"results": results})

    monkeypatch.setattr(service.httpx, "AsyncClient", lambda timeout=60.0: _Client())


def test_gemma_provider_requires_endpoint():
    with pytest.raises(ValueError, match="TRANSLATION_VLLM_BASE_URL"):
        GemmaVllmTranslationProvider(TranslationConfig(provider="gemma_vllm", model="gemma-4", endpoint=""))


def test_gemma_provider_translate():
    provider = GemmaVllmTranslationProvider(CONFIG)
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.return_value.json.return_value = {"choices": [{"message": {"content": "Translated text"}}]}

    with patch("pipeline.translation.gemma_vllm.httpx.Client", return_value=client):
        result = provider.translate("ટેસ્ટ", source_lang="gu", target_language="en")

    assert result == "Translated text"
    client.post.assert_called_once()
    assert client.post.call_args.kwargs["json"]["model"] == "gemma-4"


def test_normalize_detected_language_gujarati_script():
    assert service.normalize_detected_language("zl", "ગુજરાતી ટેક્સ્ટ") == "gu"
    assert service.normalize_detected_language("en", "English only") == "en"


def test_translate_pages_skips_english(monkeypatch):
    provider = _translator(monkeypatch, ["en", "gu"], "Gujarati content translated.")
    pages = [
        _page(1, "English content that is long enough for detection."),
        _page(2, "ગુજરાતી સામગ્રી જે અનુવાદ માટે લાંબી છે."),
    ]

    result = asyncio.run(service.translate_pages(pages, config=CONFIG))

    assert result[0].get("translated_markdown") is None
    assert result[0]["detected_language"] == "en"
    assert result[1]["translated_markdown"] == "Gujarati content translated."
    assert result[1]["detected_language"] == "gu"
    provider.translate.assert_called_once()


@pytest.mark.parametrize(
    "text",
    [
        "National Mission on Edible Oils - Oil Palm (NMEO-OP) operational guidelines.",
        "Table 3.1 | Area | Yield | 12,500 | 3.4 | Rs. 29,000 per hectare subsidy.",
        "1. Introduction\n2. Objectives\n3. Pattern of Assistance\n4. Implementation",
    ],
)
def test_english_pages_are_skipped(text):
    """The exact shape of page that was misdetected as sw/de/hu/fr/ro."""
    result = analyze_script(text)
    assert result.is_non_english is False
    assert result.language == "en"


@pytest.mark.parametrize(
    "text,expected_lang,expected_script",
    [
        (HINDI, "hi", "Devanagari"),
        (GUJARATI, "gu", "Gujarati"),
        ("இந்த திட்டத்தின் கீழ் விவசாயிகளுக்கு உதவி வழங்கப்படும்.", "ta", "Tamil"),
        ("ఈ పథకం కింద రైతులకు సహాయం అందించబడుతుంది.", "te", "Telugu"),
        ("এই প্রকল্পের অধীনে কৃষকদের সহায়তা দেওয়া হবে।", "bn", "Bengali"),
    ],
)
def test_indic_pages_are_flagged(text, expected_lang, expected_script):
    result = analyze_script(text)
    assert result.is_non_english is True
    assert result.language == expected_lang
    assert result.script == expected_script


def test_stray_glyph_does_not_trigger_translation():
    """A danda or lone character in English text must not cost a Gemma call."""
    result = analyze_script("Pattern of Assistance under the scheme is Rs. 29,000 per hectare ₹ । क")
    assert result.is_non_english is False
    assert "min_chars" in result.reason


def test_mostly_english_page_with_hindi_paragraph_is_translated():
    result = analyze_script(
        "Operational guidelines for the scheme. " * 5
        + "योजना के अंतर्गत किसानों को प्रति हेक्टेयर सहायता राशि दी जाएगी और लाभ मिलेगा।"
    )
    assert result.is_non_english is True
    assert result.language == "hi"


def test_devanagari_is_marked_ambiguous_gujarati_is_not():
    hindi, gujarati = analyze_script(HINDI), analyze_script(GUJARATI)
    assert hindi.ambiguous is True
    assert "mr" in hindi.candidates
    assert gujarati.ambiguous is False


def test_empty_page_is_english():
    assert analyze_script("").is_non_english is False
    assert analyze_script("   \n  ").is_non_english is False


def test_indic_digits_in_english_table_not_classified():
    """Localized numerals must not count as script letters (Kanav P2)."""
    arabic_indic_digits = "Table 3.1 | Area | Yield | " + "٠١٢٣٤٥٦٧٨٩" * 2 + " | subsidy norms."
    assert analyze_script(arabic_indic_digits).is_non_english is False

    devanagari_digits_only = analyze_script("०१२३४५६७८९" * 3)
    assert devanagari_digits_only.is_non_english is False
    assert "no alphabetic letters" in devanagari_digits_only.reason


def test_latin_french_page_uses_whole_page_detect(monkeypatch):
    _whole_page_detect(monkeypatch, "fr")
    french = (
        "Les bovins laitiers nécessitent une alimentation équilibrée. "
        "Cette section décrit les protocoles de vaccination et les soins vétérinaires."
    )
    detected = asyncio.run(service.detect_page_languages(_texts(french), DETECT_URL, config=GATE_CONFIG))
    assert detected == {0: "fr"}


def test_clear_machine_translation_preserves_edited_translation():
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


def test_gate_uses_whole_page_detect_for_latin_english(monkeypatch):
    """Latin-script English pages use one whole-page detect call, not per-line batch."""
    asked = _whole_page_detect(monkeypatch, "en")
    pages = _texts("Operational guidelines for oil palm.", "Pattern of assistance and subsidy norms.")
    detected = asyncio.run(service.detect_page_languages(pages, DETECT_URL, config=GATE_CONFIG))
    assert detected == {0: "en", 1: "en"}
    assert len(asked) == 2


def test_gate_logs_decision_per_page(monkeypatch):
    _whole_page_detect(monkeypatch, "en")
    messages = []

    def log(msg, *args):
        messages.append(msg % args if args else msg)

    pages = _texts("Operational guidelines for oil palm.", GUJARATI)
    detected = asyncio.run(service.detect_page_languages(pages, DETECT_URL, log=log, config=GATE_CONFIG))

    assert detected == {0: "en", 1: "gu"}
    joined = "\n".join(messages)
    assert "Page 1" in joined and ("SKIP translation" in joined or "whole-page lang-detect" in joined)
    assert "Page 2: regex" in joined and "TRANSLATE" in joined
    assert "1/2 page(s) need translation" in joined


def test_translate_pages_resume_skips_pretranslated_pages(monkeypatch):
    provider = _translator(monkeypatch, ["gu", "gu"], "fresh translation")
    pages = [
        _page(1, "ગુજરાતી પાનું એક", translated_markdown="already translated"),
        _page(2, "ગુજરાતી પાનું બે", translated_markdown=None),
    ]

    result = asyncio.run(service.translate_pages(pages, config=CONFIG))

    assert result[0]["translated_markdown"] == "already translated"
    assert result[1]["translated_markdown"] == "fresh translation"
    provider.translate.assert_called_once()


def test_translate_pages_reports_liveness_while_a_page_is_retrying(monkeypatch):
    """A page inside the retry ladder must not look idle to the heartbeat."""
    config = TranslationConfig(
        provider="gemma_vllm", model="gemma-4", endpoint="http://localhost:8000/v1", lang_detect_url=DETECT_URL,
        max_retries=3, retry_base_seconds=0.5,
    )
    # Shorter than the retry backoff so liveness events are observable.
    monkeypatch.setattr(service, "PROGRESS_LIVENESS_INTERVAL_SECONDS", 0.02)
    # The first attempt fails with a retryable error, forcing backoff.
    _translator(monkeypatch, ["gu"], [RuntimeError("connection reset"), "translated at last"])
    events: list[dict] = []

    async def _capture(event):
        events.append(event)
        await asyncio.sleep(0)

    result = asyncio.run(service.translate_pages([_page(1, "ગુજરાતી")], config=config, progress_callback=_capture))

    assert result[0]["translated_markdown"] == "translated at last"
    liveness = [e for e in events if "pages_in_flight" in e]
    assert liveness, "expected at least one liveness event during retry backoff"
    assert all(e["pages_in_flight"] == 1 for e in liveness)
    assert all("translated_page" not in e for e in liveness)
    # The completion event still arrives exactly once, with the page attached.
    completions = [e for e in events if "translated_page" in e]
    assert len(completions) == 1
    assert completions[0]["pages_completed"] == 1


def test_translate_pages_force_retranslate_overwrites_pretranslated(monkeypatch):
    provider = _translator(monkeypatch, ["gu", "gu"], ["new one", "new two"])
    pages = [
        _page(1, "ગુજરાતી પાનું એક", translated_markdown="stale translation", edited_translation="human edit"),
        _page(2, "ગુજરાતી પાનું બે", translated_markdown="stale translation 2"),
    ]

    result = asyncio.run(service.translate_pages(pages, config=CONFIG, force_retranslate=True))

    assert result[0]["translated_markdown"] == "new one"
    assert result[1]["translated_markdown"] == "new two"
    assert result[0]["edited_translation"] == "human edit"
    assert provider.translate.call_count == 2


def test_detect_page_languages_reports_progress(monkeypatch):
    _lang_detect_http(monkeypatch, [{"language": "en"}])
    events = []
    pages = _texts(
        "This is a long enough english line for detection.",
        "Another sufficiently long english line for detection.",
    )
    detected = asyncio.run(service.detect_page_languages(pages, DETECT_URL, progress_callback=events.append))

    assert detected == {0: "en", 1: "en"}
    assert len(events) == 2
    assert all(event.get("phase") == "detection" for event in events)
    assert events[-1]["pages_completed"] == 2


def test_shared_script_disambiguation_heartbeats_per_page(monkeypatch):
    """Devanagari pages must not be marked detection-complete before hi/mr HTTP."""
    timeline: list = []

    async def _slow_http():
        timeline.append("http")
        await asyncio.sleep(0.01)

    _lang_detect_http(monkeypatch, [{"language": "hi"}, {"language": "hi"}], before_reply=_slow_http)

    def _on_progress(event):
        timeline.append(("progress", event["pages_completed"], event.get("page_number")))

    detected = asyncio.run(
        service.detect_page_languages(_texts(HINDI, HINDI, HINDI), DETECT_URL, progress_callback=_on_progress)
    )

    assert detected == {0: "hi", 1: "hi", 2: "hi"}
    assert timeline == ["http", ("progress", 1, 1), "http", ("progress", 2, 2), "http", ("progress", 3, 3)]
