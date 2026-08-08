"""Chunk-quality evaluation: does a chunk still hold the answer? (#62)

Rechunking the corpus is how the broken chunk-to-document mapping gets repaired,
but a rechunk is only safe if the new chunker does not cut answers in half. This
package is the evidence for that: a gold set of questions with exact answer
offsets, and a scorer that reports whether each answer still fits inside one
chunk.

Deliberately free of the database, the API and any LLM. It takes page text,
chunk spans and gold records as plain values, so it can be run and tested
without infrastructure.
"""

from .gold import GoldRecord, capture, dump, load, page_fingerprint, validate
from .score import (
    CONTAINED,
    INVALID,
    MISSING,
    SPLIT,
    EvaluatedChunk,
    RecordResult,
    chunks_from_rows,
    is_discriminating,
    score_record,
    summarize,
    unit_intervals_for_page,
)
from .spans import covers, merge_intervals, overlaps, spans_on_page

__all__ = [
    "CONTAINED",
    "INVALID",
    "MISSING",
    "SPLIT",
    "EvaluatedChunk",
    "GoldRecord",
    "RecordResult",
    "capture",
    "chunks_from_rows",
    "covers",
    "dump",
    "is_discriminating",
    "load",
    "merge_intervals",
    "overlaps",
    "page_fingerprint",
    "score_record",
    "spans_on_page",
    "summarize",
    "unit_intervals_for_page",
    "validate",
]
