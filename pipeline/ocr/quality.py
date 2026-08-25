"""Lightweight OCR output quality checks."""

from __future__ import annotations

from collections import Counter


def is_degenerate_repetition(text: str, *, min_chars: int = 500, dominance: float = 0.90) -> bool:
    """True when OCR text is mostly one repeated character (token-cap loops)."""
    sample = "".join(ch for ch in (text or "") if not ch.isspace())
    if len(sample) < min_chars:
        return False
    char, count = Counter(sample).most_common(1)[0]
    return (count / len(sample)) >= dominance


def degenerate_ocr_note(text: str) -> str | None:
    """Human-readable note when a page looks like a repetition-loop failure."""
    if not is_degenerate_repetition(text):
        return None
    sample = "".join(ch for ch in text if not ch.isspace())
    char, count = Counter(sample).most_common(1)[0]
    ratio = (count / len(sample)) if sample else 0.0
    display = repr(char) if char not in {"\n", "\t"} else char
    return (
        f"Degenerate OCR: ~{ratio:.0%} of non-whitespace is repeated {display} "
        f"({len(sample)} chars) — review before trusting this page."
    )
