"""Tests for post-chunking token enforcement."""

import pytest

from pipeline.chunking.base import ChunkCandidate, ChunkingConfig, ChunkingResult
from pipeline.chunking.enforce_limits import (
    enforce_chunk_limits,
    enforce_marqo_byte_limit,
    enforce_max_chunk_tokens,
    estimate_marqo_tensor_payload_bytes,
)


class TestEnforceMaxChunkTokens:
    @pytest.mark.unit
    def test_splits_oversized_chunk(self):
        blob = "o" * 5000
        result = ChunkingResult(
            chunks=[
                ChunkCandidate(
                    text=blob,
                    page_start=1,
                    page_end=1,
                    source_page_numbers=[1],
                    source_spans=[{"page": 1, "start": 0, "end": len(blob)}],
                    token_count=5000,
                )
            ],
            provider="test",
            model="test",
            config=ChunkingConfig(provider="test", model="test", max_chunk_tokens=450),
        )

        out = enforce_max_chunk_tokens(result)

        assert len(out.chunks) > 1
        assert all(c.token_count <= 450 for c in out.chunks)
        assert any("Split" in w for w in out.warnings)

    @pytest.mark.unit
    def test_keeps_small_chunks(self):
        result = ChunkingResult(
            chunks=[
                ChunkCandidate(
                    text="hello world",
                    page_start=1,
                    page_end=1,
                    source_page_numbers=[1],
                    source_spans=[],
                    token_count=2,
                )
            ],
            provider="test",
            model="test",
            config=ChunkingConfig(provider="test", model="test", max_chunk_tokens=450),
        )

        out = enforce_max_chunk_tokens(result)

        assert len(out.chunks) == 1
        assert out.warnings == []


class TestEnforceMarqoByteLimit:
    @pytest.mark.unit
    def test_splits_chunk_over_marqo_byte_budget(self):
        text = "x" * 80_000
        assert estimate_marqo_tensor_payload_bytes(text) > 98_000
        result = ChunkingResult(
            chunks=[
                ChunkCandidate(
                    text=text,
                    page_start=1,
                    page_end=1,
                    source_page_numbers=[1],
                    source_spans=[],
                    token_count=1000,
                )
            ],
            provider="test",
            model="test",
            config=ChunkingConfig(provider="test", model="test"),
        )

        out = enforce_marqo_byte_limit(result)

        assert len(out.chunks) > 1
        assert all(estimate_marqo_tensor_payload_bytes(c.text) <= 98_000 for c in out.chunks)

    @pytest.mark.unit
    def test_enforce_chunk_limits_applies_both_guards(self):
        blob = "o" * 5000
        result = ChunkingResult(
            chunks=[
                ChunkCandidate(
                    text=blob,
                    page_start=1,
                    page_end=1,
                    source_page_numbers=[1],
                    source_spans=[],
                    token_count=5000,
                )
            ],
            provider="test",
            model="test",
            config=ChunkingConfig(provider="test", model="test", max_chunk_tokens=450),
        )

        out = enforce_chunk_limits(result)

        assert len(out.chunks) > 1
        assert all(c.token_count <= 450 for c in out.chunks)
