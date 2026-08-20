"""Regex script detection — decides which pages actually need translation.

The lang-detect service is run per *line* and is unreliable on the short,
noisy lines OCR produces (headings, table fragments, page numbers). A single
misdetected line used to mark a whole page non-English, so English pages were
sent to the translation model as Swahili/German/Hungarian/French/Romanian.

This module gates that decision on what the page is actually written in: the
Unicode block of its characters. Documents in this corpus are English plus
Indic scripts, and every Indic language has its own block, so a regex over
those ranges answers "is this page non-English" without a model call and
without false positives from Latin-script noise.

Script → language is 1:1 except Devanagari (Hindi/Marathi/…) and Bengali
(Bengali/Assamese); those stay ambiguous here and are handed to lang-detect to
disambiguate, but only for pages this gate has already flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Unicode blocks, in the order they are reported on ties.
# (language, script name, pattern, other languages sharing the script)
_SCRIPTS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("hi", "Devanagari", r"[ऀ-ॿ]", ("hi", "mr", "ne", "sa", "kok")),
    ("bn", "Bengali", r"[ঀ-৿]", ("bn", "as")),
    ("pa", "Gurmukhi", r"[਀-੿]", ()),
    ("gu", "Gujarati", r"[઀-૿]", ()),
    ("or", "Odia", r"[଀-୿]", ()),
    ("ta", "Tamil", r"[஀-௿]", ()),
    ("te", "Telugu", r"[ఀ-౿]", ()),
    ("kn", "Kannada", r"[ಀ-೿]", ()),
    ("ml", "Malayalam", r"[ഀ-ൿ]", ()),
    ("si", "Sinhala", r"[඀-෿]", ()),
    ("ur", "Arabic", r"[؀-ۿݐ-ݿ]", ("ur", "ar", "fa")),
)

_COMPILED = tuple((lang, name, re.compile(pat), family) for lang, name, pat, family in _SCRIPTS)

# Script code points that carry no language signal on their own: the Devanagari
# danda (।/॥) and the rupee sign show up inside otherwise-English government
# text, and would otherwise push a page over the threshold by themselves.
_NEUTRAL = re.compile(r"[।॥₹]")

DEFAULT_MIN_CHARS = 15
DEFAULT_MIN_RATIO = 0.05


@dataclass
class ScriptAnalysis:
    """Outcome of the regex gate for one page."""

    is_non_english: bool
    language: str = "en"
    script: str = "Latin"
    script_chars: int = 0
    letter_chars: int = 0
    ratio: float = 0.0
    ambiguous: bool = False
    candidates: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    def summary(self) -> str:
        """One-line, log-friendly description of the decision."""
        if not self.is_non_english:
            return (
                f"English (script={self.script} non_latin_chars={self.script_chars} "
                f"letters={self.letter_chars} ratio={self.ratio:.3f}) — {self.reason}"
            )
        return (
            f"non-English lang={self.language} script={self.script} "
            f"chars={self.script_chars} letters={self.letter_chars} ratio={self.ratio:.3f}"
            + (f" ambiguous_within={'/'.join(self.candidates)}" if self.ambiguous else "")
        )


def analyze_script(
    text: str,
    *,
    min_chars: int = DEFAULT_MIN_CHARS,
    min_ratio: float = DEFAULT_MIN_RATIO,
) -> ScriptAnalysis:
    """Classify a page as English or non-English from its character ranges.

    A page counts as non-English only when it clears *both* thresholds: an
    absolute count (so one stray glyph is not enough) and a share of all
    letters (so a mostly-English page with a single decorative word is not
    shipped off for translation).
    """
    if not text or not text.strip():
        return ScriptAnalysis(False, reason="empty page")

    scrubbed = _NEUTRAL.sub("", text)
    letters = sum(1 for ch in scrubbed if ch.isalpha())

    counts: dict[str, int] = {}
    for _lang, name, pattern, _family in _COMPILED:
        found = len(pattern.findall(scrubbed))
        if found:
            counts[name] = found

    if not counts:
        return ScriptAnalysis(
            False,
            letter_chars=letters,
            reason="no non-Latin script found",
        )

    dominant_script = max(counts, key=lambda k: counts[k])
    script_chars = counts[dominant_script]
    lang, _name, _pattern, family = next(s for s in _COMPILED if s[1] == dominant_script)

    ratio = script_chars / letters if letters else 1.0

    if script_chars < min_chars:
        return ScriptAnalysis(
            False,
            script=dominant_script,
            script_chars=script_chars,
            letter_chars=letters,
            ratio=ratio,
            reason=f"only {script_chars} {dominant_script} char(s), below min_chars={min_chars}",
        )

    if ratio < min_ratio:
        return ScriptAnalysis(
            False,
            script=dominant_script,
            script_chars=script_chars,
            letter_chars=letters,
            ratio=ratio,
            reason=f"{dominant_script} ratio {ratio:.3f} below min_ratio={min_ratio}",
        )

    return ScriptAnalysis(
        True,
        language=lang,
        script=dominant_script,
        script_chars=script_chars,
        letter_chars=letters,
        ratio=ratio,
        ambiguous=bool(family),
        candidates=family,
    )


def script_family(language: str) -> tuple[str, ...]:
    """Languages sharing a script with ``language`` (empty when unambiguous)."""
    for lang, _name, _pattern, family in _COMPILED:
        if lang == language:
            return family
    return ()
