"""Unit tests for chunking factory, adapters, and config builder."""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from pipeline.chunking.base import ChunkCandidate, ChunkingConfig, count_tokens
from pipeline.chunking.config_builder import DEFAULT_FALLBACK_PROVIDER, ChunkingConfigBuilder
from pipeline.chunking.deterministic import DeterministicChunkingProvider, _dedupe_chunks
from pipeline.chunking.factory import get_chunking_provider, is_llm_grouping_provider
from pipeline.chunking.openai_compatible import GROUPING_JSON_SCHEMA, _build_chat_payload
from pipeline.chunking.service import chunk_pages
from pipeline.config import validate_environment

FELL_BACK = "used fallback 'deterministic'"


def _env(monkeypatch, **env):
    """Set each ``env`` var, or unset it when the value is ``None``."""
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _llm_config(**overrides):
    return ChunkingConfig(**{
        "provider": "gemma_vllm", "model": "gemma-4-31b-it", "endpoint": "http://gemma.test/v1",
        "fallback_provider": "deterministic", "target_chunk_tokens": 200, "max_chunk_tokens": 300,
        "min_chunk_tokens": 50, "chunk_overlap_tokens": 0, **overrides,
    })


def _chunk_with_llm_reply(pages, config, status=200, body=None, groups=None):
    """``chunk_pages`` with the LLM endpoint answering ``status`` and ``body`` (or a reply holding ``groups``)."""
    if groups is not None:
        body = {"choices": [{"message": {"content": json.dumps({"groups": groups})}}]}
    response = httpx.Response(status, json=body, request=httpx.Request("POST", f"{config.endpoint}/chat/completions"))
    with patch("pipeline.chunking.openai_compatible.httpx.AsyncClient.post", new=AsyncMock(return_value=response)):
        return asyncio.run(chunk_pages(pages, config))


def test_factory_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unsupported chunking provider"):
        get_chunking_provider("nope_vllm")


def test_is_llm_grouping_provider():
    assert is_llm_grouping_provider("gemma_vllm")
    assert is_llm_grouping_provider("qwen_vllm")
    assert is_llm_grouping_provider("mistral_vllm")
    assert not is_llm_grouping_provider("deterministic")


GEMMA = {"CHUNKING_PROVIDER": "gemma_vllm"}


@pytest.mark.parametrize(
    "env, error",
    [
        pytest.param({"CHUNKING_PROVIDER": None, "CHUNKING_MODEL": None}, "CHUNKING_PROVIDER is required", id="provider"),
        pytest.param(
            {"CHUNKING_PROVIDER": "nope_vllm", "CHUNKING_MODEL": "x"}, "Unsupported CHUNKING_PROVIDER", id="known_provider"
        ),
        pytest.param(
            {**GEMMA, "CHUNKING_VLLM_BASE_URL": "http://gemma.test/v1", "CHUNKING_MODEL": None},
            "CHUNKING_MODEL is required", id="llm_model",
        ),
        pytest.param(
            {**GEMMA, "CHUNKING_MODEL": "gemma-4-31b-it", "CHUNKING_VLLM_BASE_URL": None},
            "CHUNKING_VLLM_BASE_URL is required", id="llm_endpoint",
        ),
    ],
)
def test_builder_requires(monkeypatch, env, error):
    _env(monkeypatch, **env)
    with pytest.raises(ValueError, match=error):
        ChunkingConfigBuilder.from_env()


def test_builder_reads_explicit_gemma_deployment(monkeypatch):
    _env(
        monkeypatch, CHUNKING_PROVIDER="gemma_vllm", CHUNKING_MODEL="gemma-4-31b-it",
        CHUNKING_VLLM_BASE_URL="http://gemma.test/v1", CHUNKING_PAGE_WINDOW_SIZE=None, CHUNKING_OVERLAP_TOKENS=None,
    )
    cfg = ChunkingConfigBuilder.from_env().build()
    assert cfg.provider == "gemma_vllm"
    assert cfg.model == "gemma-4-31b-it"
    assert cfg.fallback_provider == DEFAULT_FALLBACK_PROVIDER
    assert cfg.page_window_size == 4  # LLM default
    assert cfg.chunk_overlap_tokens <= 64


def test_builder_deterministic_without_model(monkeypatch):
    _env(
        monkeypatch, CHUNKING_PROVIDER="deterministic", CHUNKING_MODEL=None,
        CHUNKING_PAGE_WINDOW_SIZE=None, CHUNKING_OVERLAP_TOKENS=None,
    )
    cfg = ChunkingConfigBuilder.from_env().with_chunk_overlap(128).build()
    assert cfg.provider == "deterministic"
    assert cfg.model == "deterministic"
    assert cfg.page_window_size == 8
    assert cfg.chunk_overlap_tokens == 128


@pytest.mark.parametrize(
    "env, expected_errors",
    [
        pytest.param({"CHUNKING_PROVIDER": None}, ["^CHUNKING_PROVIDER:"], id="requires_chunking_provider"),
        pytest.param({"CHUNKING_PROVIDER": "nope_vllm"}, ["unsupported value 'nope_vllm'"], id="rejects_unknown"),
        pytest.param(
            {"CHUNKING_PROVIDER": "gemma_vllm", "CHUNKING_MODEL": None, "CHUNKING_VLLM_BASE_URL": None},
            ["^CHUNKING_MODEL:", "^CHUNKING_VLLM_BASE_URL:"], id="requires_llm_model_and_endpoint",
        ),
    ],
)
def test_validate_environment_chunking(monkeypatch, env, expected_errors):
    _env(monkeypatch, MINIO_ACCESS_KEY="x", MINIO_SECRET_KEY="y", **env)
    errors = validate_environment()
    for pattern in expected_errors:
        assert any(re.search(pattern, e) for e in errors), pattern


def test_raw_vllm_payload_uses_standard_json_schema_field():
    payload = _build_chat_payload(_llm_config(), "group these units", "gemma_vllm")
    assert "extra_body" not in payload
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "chunk_groups", "schema": GROUPING_JSON_SCHEMA},
    }


def test_chunk_pages_falls_back_to_deterministic():
    primary = AsyncMock()
    primary.chunk_document = AsyncMock(side_effect=RuntimeError("primary failed hard"))
    primary.name = "gemma_vllm"
    providers = {"gemma_vllm": primary, "deterministic": DeterministicChunkingProvider()}
    pages = [{"page_number": 1, "original_markdown": "Heading\n\n" + ("word " * 200)}]

    with patch("pipeline.chunking.service.get_chunking_provider", side_effect=providers.__getitem__):
        result = asyncio.run(chunk_pages(pages, _llm_config(endpoint="http://example.invalid/v1", page_window_size=4)))

    assert result.provider == "deterministic"
    assert result.chunks
    assert any(FELL_BACK in w for w in result.warnings)


def test_http_error_falls_back_with_truthful_provider_label():
    pages = [{"page_number": 1, "original_markdown": "Heading\n\n" + ("word " * 200)}]
    result = _chunk_with_llm_reply(
        pages, _llm_config(model="missing-model"), status=404, body={"error": {"message": "model not found"}}
    )
    assert result.provider == "deterministic"
    assert result.model == "deterministic"
    assert result.chunks
    assert any(FELL_BACK in warning for warning in result.warnings)


def test_overlapping_llm_groups_fall_back_to_deterministic():
    pages = [{"page_number": n, "original_markdown": f"{letter} " * 400} for n, letter in enumerate("ABC", start=1)]
    result = _chunk_with_llm_reply(
        pages, _llm_config(target_chunk_tokens=220, max_chunk_tokens=320),
        groups=[{"start_unit": 1, "end_unit": 2}, {"start_unit": 2, "end_unit": 3}],
    )
    assert result.provider == "deterministic"
    assert result.chunks
    assert any(FELL_BACK in warning for warning in result.warnings)


def test_llm_dedupe_suppresses_cross_window_adjacent_duplicates():
    pages = [{"page_number": n, "original_markdown": "Repeated boilerplate line."} for n in (1, 2)]
    config = _llm_config(
        provider="openai_vllm", model="gpt-like", endpoint="http://chunker.test/v1",
        target_chunk_tokens=20, max_chunk_tokens=80, min_chunk_tokens=1, page_window_size=1,
    )
    with patch("pipeline.chunking.openai_compatible._grouping_looks_bad", return_value=False):
        result = _chunk_with_llm_reply(
            pages, config, groups=[{"start_unit": 1, "end_unit": 1, "heading_hint": "", "is_reference": False}]
        )
    assert result.provider == "openai_vllm"
    assert len(result.chunks) == 1
    assert any("Dropped adjacent LLM chunk with identical text" in warning for warning in result.warnings)


def test_deterministic_dedupe_drops_adjacent_identical_text():
    text = "same chunk body"

    def candidate(*pages):
        return ChunkCandidate(
            text=text, page_start=pages[0], page_end=pages[-1], source_page_numbers=list(pages),
            source_spans=[], token_count=count_tokens(text),
        )

    kept, warnings = _dedupe_chunks([candidate(1, 2), candidate(1, 2, 3)])
    assert len(kept) == 1
    assert any("identical text" in warning for warning in warnings)
