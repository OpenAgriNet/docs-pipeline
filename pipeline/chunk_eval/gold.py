"""Gold records: a question plus the exact page offsets that answer it.

Ground truth here is a character interval into ``best_page_text(page)``, not a
quoted string, because the offsets survive rechunking unchanged while the text a
chunk holds does not. The quote is carried alongside so a human can review the
record, and so a drifted offset can be caught rather than silently scoring a
different piece of the page.

That drift is the failure mode worth guarding. ``best_page_text`` resolves
``edited_translation -> translated_markdown -> edited_markdown ->
original_markdown``, so a page edit or a re-run translation shifts every offset
after the edit. The gold set does not notice; it just starts reporting misses
that are stale ground truth rather than real regressions. Hence
:func:`page_fingerprint`, recorded at capture and re-checked before every run.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


def page_fingerprint(page_text: str) -> str:
    """Stable hash of a page's text, used to detect gold-set rot."""
    return hashlib.sha1((page_text or "").encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


@dataclass
class GoldRecord:
    """One question and the span of page text that answers it."""

    workflow_id: str
    question: str
    page_number: int
    start_char: int
    end_char: int
    answer_quote: str = ""
    page_text_sha1: str = ""
    notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def span(self) -> tuple[int, int]:
        return (self.start_char, self.end_char)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "GoldRecord":
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001 - dataclass API
        return cls(**{key: value for key, value in raw.items() if key in known})


def validate(record: GoldRecord, page_text: str) -> list[str]:
    """Problems that would make this record score something meaningless.

    Returns human-readable strings rather than raising, because a gold set is
    reviewed in bulk: the useful output is every bad record at once, not the
    first one.

    The quote check is deliberately whitespace-insensitive. A reviewer pasting a
    quote out of a rendered document reflows it, and that should not fail a
    record whose offsets are correct.
    """
    problems: list[str] = []

    if record.end_char <= record.start_char:
        problems.append(
            f"empty or inverted span [{record.start_char}, {record.end_char})"
        )
    if record.start_char < 0:
        problems.append(f"negative start_char {record.start_char}")
    if record.end_char > len(page_text):
        problems.append(
            f"span ends at {record.end_char} but page {record.page_number} "
            f"is {len(page_text)} chars"
        )

    if record.page_text_sha1:
        actual = page_fingerprint(page_text)
        if actual != record.page_text_sha1:
            problems.append(
                "page text has changed since capture "
                f"(recorded {record.page_text_sha1[:12]}, now {actual[:12]}) — "
                "offsets can no longer be trusted"
            )

    if record.answer_quote and not problems:
        actual_quote = page_text[record.start_char:record.end_char]
        if _normalize(actual_quote) != _normalize(record.answer_quote):
            problems.append(
                "answer_quote does not match the text at those offsets "
                f"(offsets hold {_normalize(actual_quote)[:60]!r})"
            )

    if not record.question.strip():
        problems.append("empty question")

    return problems


def capture(
    workflow_id: str,
    question: str,
    page_number: int,
    page_text: str,
    start_char: int,
    end_char: int,
    **extra: Any,
) -> GoldRecord:
    """Build a record from live page text, filling in quote and fingerprint."""
    return GoldRecord(
        workflow_id=workflow_id,
        question=question,
        page_number=page_number,
        start_char=start_char,
        end_char=end_char,
        answer_quote=page_text[start_char:end_char],
        page_text_sha1=page_fingerprint(page_text),
        **extra,
    )


def load(path: str | Path) -> list[GoldRecord]:
    """Read a gold set from JSONL — one record per line, diffable in review."""
    records: list[GoldRecord] = []
    with open(path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(GoldRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                # Incomplete records raise TypeError from the dataclass; bad JSON
                # raises JSONDecodeError. Both should name the offending line so a
                # reviewer can jump straight there.
                raise ValueError(f"{path}:{line_number}: {error}") from error
    return records


def dump(records: Iterable[GoldRecord], path: str | Path) -> int:
    """Write a gold set as JSONL. Returns the number of records written."""
    written = 0
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            written += 1
    return written
