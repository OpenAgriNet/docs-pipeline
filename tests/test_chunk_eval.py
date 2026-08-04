"""Chunk-quality scorer contract (#62).

The scorer decides whether a rechunk is safe to run against the corpus, so the
behaviours it would be dangerous to get wrong are pinned here:

* coverage bridges the whitespace gaps between units, because spans do not cover
  the blank lines that separate them and a strict reading would call every
  multi-paragraph answer a miss;
* it does *not* bridge a gap holding real text, because that is the split the
  metric exists to find;
* a stale gold record scores ``invalid``, never ``missing`` — drifted offsets
  must not be readable as a chunker regression;
* records that cannot discriminate are counted separately, since both providers
  group the same units and an answer inside one unit is contained no matter what.
"""

from __future__ import annotations

import json

import pytest

from pipeline.chunk_eval import (
    CONTAINED,
    INVALID,
    MISSING,
    SPLIT,
    EvaluatedChunk,
    GoldRecord,
    capture,
    chunks_from_rows,
    covers,
    dump,
    is_discriminating,
    load,
    merge_intervals,
    page_fingerprint,
    score_record,
    spans_on_page,
    summarize,
    unit_intervals_for_page,
    validate,
)


def _span(page, start, end):
    return {"page_number": page, "start_char": start, "end_char": end}


# --------------------------------------------------------------------- spans


def test_merge_bridges_the_blank_line_between_two_units():
    """Units are split on blank lines and no span covers the separator, so a
    chunk's spans are only contiguous if whitespace gaps are bridged."""
    text = "alpha\n\nbeta"
    # "alpha" = [0,5), "beta" = [7,11); [5,7) is the blank line, covered by neither.
    assert merge_intervals([(0, 5), (7, 11)], text) == [(0, 11)]


def test_merge_refuses_to_bridge_a_gap_with_real_text():
    text = "alpha REAL beta"
    assert merge_intervals([(0, 5), (11, 15)], text) == [(0, 5), (11, 15)]


def test_covers_spans_a_paragraph_boundary():
    text = "alpha\n\nbeta"
    assert covers([(0, 5), (7, 11)], text, 3, 9) is True


def test_covers_is_false_across_a_gap_holding_content():
    text = "alpha REAL beta"
    assert covers([(0, 5), (11, 15)], text, 3, 13) is False


def test_covers_rejects_an_empty_interval():
    assert covers([(0, 10)], "0123456789", 5, 5) is False


def test_spans_on_page_narrows_to_one_page_and_drops_junk():
    spans = [_span(1, 0, 5), _span(2, 0, 5), {"page_number": 1}, "nonsense", _span(1, 9, 9)]
    assert spans_on_page(spans, 1) == [(0, 5)]


# ---------------------------------------------------------------------- gold


def test_capture_fills_in_the_quote_and_fingerprint():
    page = "The booster is given every six months."
    record = capture("doc-1", "How often?", 3, page, 4, 10)

    assert record.answer_quote == "booste"
    assert record.page_text_sha1 == page_fingerprint(page)
    assert validate(record, page) == []


def test_validate_catches_a_page_that_changed_under_the_gold_set():
    """The dangerous failure: an edited page shifts every offset after it, and
    the harness reports misses that are stale ground truth, not regressions."""
    record = capture("doc-1", "q", 1, "original text here", 0, 8)

    problems = validate(record, "PREFIX original text here")

    assert problems
    assert any("page text has changed" in p for p in problems)


def test_validate_catches_offsets_past_the_end_of_the_page():
    record = GoldRecord("doc-1", "q", 1, 5, 500)
    assert any("is 20 chars" in p for p in validate(record, "x" * 20))


def test_validate_catches_an_inverted_span():
    assert any("inverted" in p for p in validate(GoldRecord("d", "q", 1, 9, 4), "x" * 20))


def test_validate_tolerates_a_reflowed_quote():
    """A reviewer pasting from a rendered document reflows whitespace; that must
    not fail a record whose offsets are right."""
    page = "line one\nline two"
    record = GoldRecord("d", "q", 1, 0, 17, answer_quote="line one line two")
    assert validate(record, page) == []


def test_validate_catches_a_quote_that_points_somewhere_else():
    page = "alpha beta gamma"
    record = GoldRecord("d", "q", 1, 0, 5, answer_quote="gamma")
    assert any("does not match" in p for p in validate(record, page))


def test_gold_set_round_trips_through_jsonl(tmp_path):
    path = tmp_path / "gold.jsonl"
    records = [capture("doc-1", "q1", 1, "page one text", 0, 4),
               capture("doc-2", "q2", 2, "page two text", 5, 8)]

    assert dump(records, path) == 2
    assert [r.to_dict() for r in load(path)] == [r.to_dict() for r in records]


def test_load_reports_the_offending_line(tmp_path):
    path = tmp_path / "bad.jsonl"
    # Line 1 is a complete record; line 2 is the broken one.
    good = capture("doc-1", "q", 1, "page text here", 0, 4)
    path.write_text(
        json.dumps(good.to_dict(), sort_keys=True) + "\nnot json\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bad.jsonl:2"):
        load(path)


def test_load_reports_an_incomplete_record_line(tmp_path):
    path = tmp_path / "incomplete.jsonl"
    path.write_text('{"workflow_id": "a"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="incomplete.jsonl:1"):
        load(path)


# --------------------------------------------------------------------- score


def test_answer_inside_one_chunk_is_contained():
    page = "alpha\n\nbeta\n\ngamma"
    chunks = [EvaluatedChunk(1, [_span(1, 0, 5), _span(1, 7, 11)]),
              EvaluatedChunk(2, [_span(1, 13, 18)])]
    record = capture("d", "q", 1, page, 0, 11)

    result = score_record(record, page, chunks)

    assert result.verdict == CONTAINED
    assert result.chunk_number == 1


def test_answer_across_a_chunk_boundary_is_split_not_missing():
    """The distinction the comparison turns on: the content is all present, a
    boundary just cut it in half."""
    page = "alpha\n\nbeta\n\ngamma"
    chunks = [EvaluatedChunk(1, [_span(1, 0, 5)]), EvaluatedChunk(2, [_span(1, 7, 18)])]
    record = capture("d", "q", 1, page, 3, 9)

    result = score_record(record, page, chunks)

    assert result.verdict == SPLIT
    assert result.touching_chunks == [1, 2]


def test_answer_no_chunk_holds_is_missing():
    """Worse than a bad boundary and not the chunker's fault, so it must not be
    averaged into the split rate."""
    page = "alpha\n\nbeta\n\ngamma"
    chunks = [EvaluatedChunk(1, [_span(1, 0, 5)])]
    record = capture("d", "q", 1, page, 13, 18)

    result = score_record(record, page, chunks)

    assert result.verdict == MISSING
    assert result.touching_chunks == []


def test_a_chunk_on_another_page_cannot_satisfy_a_record():
    page = "alpha beta gamma"
    chunks = [EvaluatedChunk(1, [_span(7, 0, 16)])]
    record = capture("d", "q", 1, page, 0, 5)

    assert score_record(record, page, chunks).verdict == MISSING


def test_a_stale_record_scores_invalid_rather_than_missing():
    """Otherwise gold-set rot is indistinguishable from a chunker regression."""
    page = "PREFIX alpha beta"
    record = GoldRecord("d", "q", 1, 0, 5, page_text_sha1="stale" * 8)

    result = score_record(record, page, [EvaluatedChunk(1, [_span(1, 0, 17)])])

    assert result.verdict == INVALID
    assert result.problems


def test_chunks_from_rows_survives_missing_and_broken_span_json():
    """Legacy chunks are expected to have no spans; the scorer must still run
    and report on them rather than refusing to start."""
    chunks = chunks_from_rows([
        {"chunk_number": 1, "source_spans_json": json.dumps([_span(1, 0, 5)])},
        {"chunk_number": 2, "source_spans_json": None},
        {"chunk_number": 3, "source_spans_json": "{not json"},
    ])

    assert [c.chunk_number for c in chunks] == [1, 2, 3]
    assert chunks[0].source_spans == [_span(1, 0, 5)]
    assert chunks[1].source_spans == [] and chunks[2].source_spans == []


def test_a_corpus_with_no_spans_scores_missing_not_contained():
    """Fail loud: if legacy chunks turn out to carry no spans, the harness must
    say so rather than quietly reporting a perfect score."""
    page = "alpha beta gamma"
    chunks = chunks_from_rows([{"chunk_number": 1, "source_spans_json": None}])

    assert score_record(capture("d", "q", 1, page, 0, 5), page, chunks).verdict == MISSING


# ------------------------------------------------------------- discrimination


def test_an_answer_inside_one_unit_cannot_discriminate():
    """Both providers group the same units, so no grouping can split this."""
    page = "alpha beta gamma"
    units = unit_intervals_for_page(page)
    record = capture("d", "q", 1, page, 0, 5)

    assert is_discriminating(record, page, units) is False


def test_an_answer_crossing_a_unit_boundary_can_discriminate():
    page = "first paragraph\n\nsecond paragraph"
    units = unit_intervals_for_page(page)
    record = capture("d", "q", 1, page, 6, 22)  # spans the blank line

    assert is_discriminating(record, page, units) is True


def test_unit_intervals_follow_paragraph_breaks():
    intervals = unit_intervals_for_page("one\n\ntwo\n\nthree")
    assert len(intervals) == 3


def test_summary_separates_the_discriminating_subset():
    """A gold set of non-discriminating records scores ~100% for every chunker.
    Reporting one blended number would read as "both chunkers are excellent"."""
    page = "alpha\n\nbeta"
    contained = capture("d", "q", 1, page, 0, 5)
    split = capture("d", "q", 1, page, 3, 9)
    chunks = [EvaluatedChunk(1, [_span(1, 0, 5)]), EvaluatedChunk(2, [_span(1, 7, 11)])]

    results = [
        score_record(contained, page, chunks, unit_intervals=[(0, 5), (7, 11)]),
        score_record(split, page, chunks, unit_intervals=[(0, 5), (7, 11)]),
    ]
    summary = summarize(results)

    assert summary["overall"]["split_rate"] == 0.5
    # Only the boundary-crossing record could have moved, and it failed.
    assert summary["discriminating_count"] == 1
    assert summary["discriminating"]["split_rate"] == 1.0


def test_invalid_records_are_excluded_from_the_rates():
    page = "alpha beta"
    good = capture("d", "q", 1, page, 0, 5)
    stale = GoldRecord("d", "q", 1, 0, 5, page_text_sha1="nope" * 10)
    chunks = [EvaluatedChunk(1, [_span(1, 0, 10)])]

    summary = summarize([score_record(r, page, chunks) for r in (good, stale)])

    assert summary["overall"][INVALID] == 1
    assert summary["overall"]["scored"] == 1
    assert summary["overall"]["contained_rate"] == 1.0


# ------------------------------------------------- end-to-end, real providers


@pytest.mark.asyncio
async def test_metric_responds_to_a_real_chunking_run():
    """Score against chunks the deterministic provider actually produced.

    Guards the coordinate system end to end: gold offsets are taken against
    `best_page_text`, and the spans come back through `split_page_into_units` and
    `merge_units`. If those ever stop agreeing, every score silently becomes
    meaningless, and no unit test on synthetic spans would notice.
    """
    from pipeline.chunking.base import ChunkingConfig
    from pipeline.chunking.deterministic import DeterministicChunkingProvider

    # Several short pages so flush produces multiple chunks and the same-page
    # merge pass cannot glue them all back together (it always merges adjacent
    # chunks that share a page, regardless of max_chunk_tokens).
    pages = [
        {
            "page_number": i + 1,
            "original_markdown": (
                f"Paragraph {i}A with enough words to count as a unit.\n\n"
                f"Paragraph {i}B with enough words to count as a unit."
            ),
        }
        for i in range(8)
    ]
    page_text = pages[0]["original_markdown"]

    config = ChunkingConfig(
        provider="deterministic",
        model="deterministic",
        target_chunk_tokens=40,
        max_chunk_tokens=40,
        min_chunk_tokens=10,
        chunk_overlap_tokens=0,
        max_pages_per_chunk=1,
    )
    result = await DeterministicChunkingProvider().chunk_document(pages, config)
    chunks = [
        EvaluatedChunk(idx, chunk.source_spans)
        for idx, chunk in enumerate(result.chunks, 1)
    ]

    assert len(chunks) > 1, "need several chunks for the metric to mean anything"

    record = capture("d", "which paragraph?", 1, page_text, 0, len("Paragraph 0A"))
    outcome = score_record(record, page_text, chunks)

    assert outcome.verdict == CONTAINED
    assert outcome.problems == []
