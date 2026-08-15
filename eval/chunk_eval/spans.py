"""Interval arithmetic over chunk source spans.

A chunk records where it came from as ``source_spans`` — ``(page_number,
start_char, end_char)`` offsets into ``best_page_text(page)``. Asking "does this
chunk still hold the answer to that question" is therefore an interval question,
not a string one, and this module is the arithmetic.

Two properties of how spans are produced drive everything here.

**Spans have holes.** ``split_page_into_units`` slices each block with
``piece_end = piece_start + len(piece)``, so a span covers its slice of a block —
but the blank lines *between* blocks belong to no span at all. A chunk built from
three paragraphs has three spans with two gaps, and those gaps are whitespace in
the page text. Asking for one span that contains the answer would therefore call
almost every multi-paragraph answer a miss. Coverage is a union, and a gap is
bridged when the page text across it is whitespace-only.

**String matching is not an alternative.** ``merge_units`` strips each unit and
joins with ``"\\n\\n"``, ``edited_text`` may override ``original_text`` after
review, and ``clean_text_for_ingestion`` normalises again on the way to the
index. A chunk's text is not a substring of its page. Its spans are exact.
"""

from __future__ import annotations

from typing import Iterable, Optional

# (start_char, end_char), end-exclusive, as offsets into one page's text.
Interval = tuple[int, int]


def spans_on_page(source_spans: Iterable[dict], page_number: int) -> list[Interval]:
    """Intervals from ``source_spans`` that land on ``page_number``.

    A chunk may span several pages; an answer lives on exactly one, so scoring
    always narrows to a single page first.
    """
    intervals: list[Interval] = []
    for span in source_spans or []:
        if not isinstance(span, dict):
            continue
        if span.get("page_number") != page_number:
            continue
        start, end = span.get("start_char"), span.get("end_char")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        if end > start:
            intervals.append((start, end))
    return intervals


def merge_intervals(intervals: Iterable[Interval], page_text: str) -> list[Interval]:
    """Coalesce overlapping intervals, bridging whitespace-only gaps.

    The bridging is the point. Consecutive units are separated in the page by the
    blank line that split them, which no span covers, so without it a chunk's
    coverage would be as fragmented as its unit list.

    A gap is bridged only when the page text across it is entirely whitespace: a
    gap holding real characters means content genuinely absent from this chunk,
    and merging across it would report coverage the chunk does not have.
    """
    ordered = sorted(intervals)
    if not ordered:
        return []

    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # overlapping or exactly adjacent
            merged[-1] = (last_start, max(last_end, end))
        elif not page_text[last_end:start].strip():
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def covers(
    intervals: Iterable[Interval],
    page_text: str,
    start: int,
    end: int,
) -> bool:
    """True when ``intervals`` together cover ``[start, end)``.

    "Together" in the :func:`merge_intervals` sense — one merged run has to hold
    the whole answer. An answer covered only by two runs with real text between
    them is exactly the split this metric exists to detect, so it is not coverage.
    """
    if end <= start:
        return False
    for run_start, run_end in merge_intervals(intervals, page_text):
        if run_start <= start and end <= run_end:
            return True
    return False


def covering_run(
    intervals: Iterable[Interval],
    page_text: str,
    start: int,
    end: int,
) -> Optional[Interval]:
    """The merged run covering ``[start, end)``, for reporting. ``None`` if split."""
    if end <= start:
        return None
    for run in merge_intervals(intervals, page_text):
        if run[0] <= start and end <= run[1]:
            return run
    return None


def overlaps(intervals: Iterable[Interval], start: int, end: int) -> bool:
    """True when any interval intersects ``[start, end)`` at all.

    Distinct from :func:`covers`: this is how a split answer's chunks are found,
    since each holds a piece and none holds all of it.
    """
    return any(i_start < end and start < i_end for i_start, i_end in intervals)
