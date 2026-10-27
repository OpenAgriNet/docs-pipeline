"""Tests for post-chunking token enforcement."""


from pipeline.chunking.base import ChunkCandidate, ChunkingConfig, ChunkingResult
from pipeline.chunking.enforce_limits import (
    enforce_chunk_limits,
    enforce_marqo_byte_limit,
    enforce_max_chunk_tokens,
    estimate_marqo_tensor_payload_bytes,
)


def _result(text, token_count, source_spans=(), **config):
    """A one-chunk, one-page ``ChunkingResult``; ``config`` goes to ``ChunkingConfig``."""
    chunk = ChunkCandidate(
        text=text, page_start=1, page_end=1, source_page_numbers=[1],
        source_spans=list(source_spans), token_count=token_count,
    )
    return ChunkingResult(
        chunks=[chunk], provider="test", model="test", config=ChunkingConfig(provider="test", model="test", **config)
    )


def test_splits_oversized_chunk():
    blob = "o" * 5000
    spans = [{"page": 1, "start": 0, "end": len(blob)}]
    out = enforce_max_chunk_tokens(_result(blob, 5000, spans, max_chunk_tokens=450))
    assert len(out.chunks) > 1
    assert all(c.token_count <= 450 for c in out.chunks)
    assert any("Split" in w for w in out.warnings)


def test_keeps_small_chunks():
    out = enforce_max_chunk_tokens(_result("hello world", 2, max_chunk_tokens=450))
    assert len(out.chunks) == 1
    assert out.warnings == []


def test_splits_chunk_over_marqo_byte_budget():
    text = "x" * 80_000
    assert estimate_marqo_tensor_payload_bytes(text) > 98_000
    out = enforce_marqo_byte_limit(_result(text, 1000))
    assert len(out.chunks) > 1
    assert all(estimate_marqo_tensor_payload_bytes(c.text) <= 98_000 for c in out.chunks)


def test_enforce_chunk_limits_applies_both_guards():
    split = enforce_chunk_limits(_result("o" * 5000, 5000, max_chunk_tokens=450))
    assert len(split.chunks) > 1
    assert all(c.token_count <= 450 for c in split.chunks)

    # Under the token cap by its own count, over Marqo's byte budget.
    big = enforce_chunk_limits(_result("x" * 80_000, 400, max_chunk_tokens=450))
    assert len(big.chunks) > 1
    assert all(estimate_marqo_tensor_payload_bytes(c.text) <= 98_000 for c in big.chunks)
