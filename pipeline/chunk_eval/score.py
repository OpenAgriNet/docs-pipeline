"""Scoring a gold set against a set of chunks.

Three outcomes, because two would hide the distinction that matters:

``contained``
    One chunk covers the whole answer. What we want.
``split``
    No single chunk covers it, but the chunks together do. The answer survived
    ingestion and was then cut in half by a boundary — the specific damage a
    chunker does, and the number a chunker comparison turns on.
``missing``
    Not even the union covers it. Content was dropped somewhere upstream. A
    worse bug than a bad boundary, and not the chunker's fault, so it must not
    be averaged into the same figure.

**Not every record can discriminate.** ``deterministic`` and ``qwen_vllm`` both
build chunks by grouping units from the same ``split_page_into_units``; neither
rewrites text. A chunker can therefore only split an answer that crosses a *unit*
boundary — anything inside one unit is contained under every possible grouping
and scores 100% for every provider.

Since units break at paragraph boundaries and at a hard
``max(600, max_chunk_tokens * 4.2)`` character slice, a gold set of
single-paragraph answers would score identically for every chunker and compare
nothing. :func:`is_discriminating` marks which records can actually move, and
:func:`summarize` reports the full set and the discriminating subset separately:
the first is what production experiences, the second is what compares chunkers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from .gold import GoldRecord, validate
from .spans import covers, overlaps, spans_on_page

CONTAINED = "contained"
SPLIT = "split"
MISSING = "missing"
INVALID = "invalid"


@dataclass
class EvaluatedChunk:
    """The part of a chunk this metric needs: its identity and its provenance."""

    chunk_number: int
    source_spans: list[dict] = field(default_factory=list)


def chunks_from_rows(rows: Iterable[dict]) -> list[EvaluatedChunk]:
    """Adapt ``chunks`` table rows, whose spans are a JSON string column.

    A row with unparseable or absent ``source_spans_json`` becomes a chunk with
    no spans rather than an error: that is exactly the state legacy chunks are
    expected to be in, and the scorer should be able to report on it instead of
    refusing to run.
    """
    chunks: list[EvaluatedChunk] = []
    for row in rows:
        raw = row.get("source_spans_json")
        spans: list[dict] = []
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    spans = [span for span in parsed if isinstance(span, dict)]
            except (json.JSONDecodeError, TypeError):
                spans = []
        chunks.append(
            EvaluatedChunk(chunk_number=row.get("chunk_number", 0), source_spans=spans)
        )
    return chunks


@dataclass
class RecordResult:
    """How one gold record fared against one set of chunks."""

    record: GoldRecord
    verdict: str
    chunk_number: Optional[int] = None
    touching_chunks: list[int] = field(default_factory=list)
    discriminating: Optional[bool] = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.verdict == CONTAINED


def score_record(
    record: GoldRecord,
    page_text: str,
    chunks: Sequence[EvaluatedChunk],
    *,
    unit_intervals: Optional[Sequence[tuple[int, int]]] = None,
) -> RecordResult:
    """Classify one record as contained, split or missing.

    A record that fails validation scores ``invalid`` rather than counting as a
    miss. Stale offsets would otherwise look exactly like a chunker regression,
    which is the most expensive way for this harness to be wrong.
    """
    problems = validate(record, page_text)
    if problems:
        return RecordResult(record=record, verdict=INVALID, problems=problems)

    start, end = record.span
    discriminating = (
        is_discriminating(record, page_text, unit_intervals)
        if unit_intervals is not None
        else None
    )

    per_chunk = [
        (chunk.chunk_number, spans_on_page(chunk.source_spans, record.page_number))
        for chunk in chunks
    ]

    for chunk_number, intervals in per_chunk:
        if covers(intervals, page_text, start, end):
            return RecordResult(
                record=record,
                verdict=CONTAINED,
                chunk_number=chunk_number,
                touching_chunks=[chunk_number],
                discriminating=discriminating,
            )

    touching = [num for num, intervals in per_chunk if overlaps(intervals, start, end)]
    everything = [span for _, intervals in per_chunk for span in intervals]
    verdict = SPLIT if covers(everything, page_text, start, end) else MISSING

    return RecordResult(
        record=record,
        verdict=verdict,
        touching_chunks=sorted(touching),
        discriminating=discriminating,
    )


def is_discriminating(
    record: GoldRecord,
    page_text: str,
    unit_intervals: Sequence[tuple[int, int]],
) -> bool:
    """True when a chunker's grouping choice could affect this record.

    False means the answer sits inside a single unit, so every grouping contains
    it and the record scores the same for every provider. Such records are not
    useless — they still measure whether content survived ingestion — but they
    cannot compare chunkers, and averaging them in dilutes the comparison toward
    100% no matter how bad a chunker is.
    """
    start, end = record.span
    return not any(
        covers([interval], page_text, start, end) for interval in unit_intervals
    )


def unit_intervals_for_page(
    page_text: str,
    page_number: int = 1,
    config: Optional[Any] = None,
) -> list[tuple[int, int]]:
    """Unit boundaries a page would produce, for the discrimination probe.

    Imported lazily so this module stays usable — and unit-testable — without
    pulling in tiktoken and the chunking stack.
    """
    from ..chunking.base import ChunkingConfig
    from ..chunking.page_units import split_page_into_units

    config = config or ChunkingConfig(provider="deterministic", model="deterministic")
    units = split_page_into_units(page_number, page_text, config)
    return [
        (span["start_char"], span["end_char"])
        for unit in units
        for span in unit["source_spans"]
    ]


def summarize(results: Sequence[RecordResult]) -> dict:
    """Aggregate counts and rates, over the full set and the discriminating subset.

    ``split_rate`` is reported against contained+split+missing but *not* invalid,
    since an invalid record measures the gold set rather than the chunker.
    """

    def tally(subset: Sequence[RecordResult]) -> dict:
        counts = {
            CONTAINED: 0,
            SPLIT: 0,
            MISSING: 0,
            INVALID: 0,
        }
        for result in subset:
            counts[result.verdict] = counts.get(result.verdict, 0) + 1
        scored = counts[CONTAINED] + counts[SPLIT] + counts[MISSING]
        return {
            **counts,
            "scored": scored,
            "contained_rate": counts[CONTAINED] / scored if scored else None,
            "split_rate": counts[SPLIT] / scored if scored else None,
            "missing_rate": counts[MISSING] / scored if scored else None,
        }

    discriminating = [r for r in results if r.discriminating]
    known = [r for r in results if r.discriminating is not None]

    return {
        "total": len(results),
        "overall": tally(results),
        "discriminating": tally(discriminating),
        "discriminating_count": len(discriminating),
        "discriminating_share": (
            len(discriminating) / len(known) if known else None
        ),
    }
