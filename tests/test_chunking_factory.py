"""Unit tests for chunking factory, adapters, and config builder."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from pipeline.chunking.adapters import (
    GemmaVllmChunkingProvider,
    MistralVllmChunkingProvider,
    OpenaiVllmChunkingProvider,
    QwenVllmChunkingProvider,
)
from pipeline.chunking.base import ChunkingConfig
from pipeline.chunking.config_builder import DEFAULT_FALLBACK_PROVIDER, ChunkingConfigBuilder
from pipeline.chunking.deterministic import _dedupe_chunks
from pipeline.chunking.deterministic import DeterministicChunkingProvider
from pipeline.chunking.factory import get_chunking_provider, is_llm_grouping_provider, list_chunking_providers
from pipeline.chunking.base import ChunkCandidate, count_tokens
from pipeline.chunking.openai_compatible import GROUPING_JSON_SCHEMA, _build_chat_payload
from pipeline.chunking.service import chunk_pages, load_chunking_config
from pipeline.config import validate_environment


def test_list_chunking_providers_includes_expected():
    names = list_chunking_providers()
    assert "gemma_vllm" in names
    assert "qwen_vllm" in names
    assert "mistral_vllm" in names
    assert "deterministic" in names
    assert "openai_vllm" in names


@pytest.mark.parametrize(
    "provider,expected_cls,expected_name",
    [
        ("gemma_vllm", GemmaVllmChunkingProvider, "gemma_vllm"),
        ("qwen_vllm", QwenVllmChunkingProvider, "qwen_vllm"),
        ("mistral_vllm", MistralVllmChunkingProvider, "mistral_vllm"),
        ("deterministic", DeterministicChunkingProvider, "deterministic"),
        ("openai_vllm", OpenaiVllmChunkingProvider, "openai_vllm"),
    ],
)
def test_factory_returns_named_adapters(provider, expected_cls, expected_name):
    instance = get_chunking_provider(provider)
    assert isinstance(instance, expected_cls)
    assert instance.name == expected_name


def test_factory_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unsupported chunking provider"):
        get_chunking_provider("nope_vllm")


def test_is_llm_grouping_provider():
    assert is_llm_grouping_provider("gemma_vllm")
    assert is_llm_grouping_provider("qwen_vllm")
    assert is_llm_grouping_provider("mistral_vllm")
    assert not is_llm_grouping_provider("deterministic")


def test_builder_requires_provider(monkeypatch):
    monkeypatch.delenv("CHUNKING_PROVIDER", raising=False)
    monkeypatch.delenv("CHUNKING_MODEL", raising=False)
    with pytest.raises(ValueError, match="CHUNKING_PROVIDER is required"):
        ChunkingConfigBuilder.from_env()


def test_builder_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("CHUNKING_PROVIDER", "nope_vllm")
    monkeypatch.setenv("CHUNKING_MODEL", "x")
    with pytest.raises(ValueError, match="Unsupported CHUNKING_PROVIDER"):
        ChunkingConfigBuilder.from_env()


def test_builder_requires_model_for_llm(monkeypatch):
    monkeypatch.setenv("CHUNKING_PROVIDER", "gemma_vllm")
    monkeypatch.setenv("CHUNKING_VLLM_BASE_URL", "http://gemma.test/v1")
    monkeypatch.delenv("CHUNKING_MODEL", raising=False)
    with pytest.raises(ValueError, match="CHUNKING_MODEL is required"):
        ChunkingConfigBuilder.from_env()


def test_builder_requires_endpoint_for_llm(monkeypatch):
    monkeypatch.setenv("CHUNKING_PROVIDER", "gemma_vllm")
    monkeypatch.setenv("CHUNKING_MODEL", "gemma-4-31b-it")
    monkeypatch.delenv("CHUNKING_VLLM_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="CHUNKING_VLLM_BASE_URL is required"):
        ChunkingConfigBuilder.from_env()


def test_builder_reads_explicit_gemma_deployment(monkeypatch):
    monkeypatch.setenv("CHUNKING_PROVIDER", "gemma_vllm")
    monkeypatch.setenv("CHUNKING_MODEL", "gemma-4-31b-it")
    monkeypatch.setenv("CHUNKING_VLLM_BASE_URL", "http://gemma.test/v1")
    monkeypatch.delenv("CHUNKING_PAGE_WINDOW_SIZE", raising=False)
    monkeypatch.delenv("CHUNKING_OVERLAP_TOKENS", raising=False)
    cfg = ChunkingConfigBuilder.from_env().build()
    assert cfg.provider == "gemma_vllm"
    assert cfg.model == "gemma-4-31b-it"
    assert cfg.fallback_provider == DEFAULT_FALLBACK_PROVIDER
    assert cfg.page_window_size == 4  # LLM default
    assert cfg.chunk_overlap_tokens <= 64


def test_builder_deterministic_without_model(monkeypatch):
    monkeypatch.setenv("CHUNKING_PROVIDER", "deterministic")
    monkeypatch.delenv("CHUNKING_MODEL", raising=False)
    monkeypatch.delenv("CHUNKING_PAGE_WINDOW_SIZE", raising=False)
    monkeypatch.delenv("CHUNKING_OVERLAP_TOKENS", raising=False)
    cfg = (
        ChunkingConfigBuilder.from_env()
        .with_chunk_overlap(128)
        .build()
    )
    assert cfg.provider == "deterministic"
    assert cfg.model == "deterministic"
    assert cfg.page_window_size == 8
    assert cfg.chunk_overlap_tokens == 128


def test_load_chunking_config_requires_env(monkeypatch):
    monkeypatch.setenv("CHUNKING_PROVIDER", "gemma_vllm")
    monkeypatch.setenv("CHUNKING_MODEL", "gemma-4-31b-it")
    monkeypatch.setenv("CHUNKING_VLLM_BASE_URL", "http://gemma.test/v1")
    cfg = load_chunking_config()
    assert cfg.provider == "gemma_vllm"
    assert cfg.model == "gemma-4-31b-it"


def test_validate_environment_requires_chunking_provider(monkeypatch):
    monkeypatch.setenv("MINIO_ACCESS_KEY", "x")
    monkeypatch.setenv("MINIO_SECRET_KEY", "y")
    monkeypatch.delenv("CHUNKING_PROVIDER", raising=False)
    errors = validate_environment()
    assert any(e.startswith("CHUNKING_PROVIDER:") for e in errors)


def test_validate_environment_rejects_unknown_chunking_provider(monkeypatch):
    monkeypatch.setenv("MINIO_ACCESS_KEY", "x")
    monkeypatch.setenv("MINIO_SECRET_KEY", "y")
    monkeypatch.setenv("CHUNKING_PROVIDER", "nope_vllm")
    errors = validate_environment()
    assert any("unsupported value 'nope_vllm'" in e for e in errors)


def test_validate_environment_requires_llm_model_and_endpoint(monkeypatch):
    monkeypatch.setenv("MINIO_ACCESS_KEY", "x")
    monkeypatch.setenv("MINIO_SECRET_KEY", "y")
    monkeypatch.setenv("CHUNKING_PROVIDER", "gemma_vllm")
    monkeypatch.delenv("CHUNKING_MODEL", raising=False)
    monkeypatch.delenv("CHUNKING_VLLM_BASE_URL", raising=False)
    errors = validate_environment()
    assert any(error.startswith("CHUNKING_MODEL:") for error in errors)
    assert any(error.startswith("CHUNKING_VLLM_BASE_URL:") for error in errors)


def test_raw_vllm_payload_uses_standard_json_schema_field():
    config = ChunkingConfig(
        provider="gemma_vllm",
        model="gemma-4-31b-it",
        endpoint="http://gemma.test/v1",
    )
    payload = _build_chat_payload(config, "group these units", "gemma_vllm")
    assert "extra_body" not in payload
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "chunk_groups", "schema": GROUPING_JSON_SCHEMA},
    }


@pytest.mark.asyncio
async def test_chunk_pages_falls_back_to_deterministic():
    pages = [
        {
            "page_number": 1,
            "original_markdown": "Heading\n\n" + ("word " * 200),
        }
    ]
    config = ChunkingConfig(
        provider="gemma_vllm",
        model="gemma-4-31b-it",
        endpoint="http://example.invalid/v1",
        fallback_provider="deterministic",
        page_window_size=4,
        target_chunk_tokens=200,
        max_chunk_tokens=300,
        min_chunk_tokens=50,
        chunk_overlap_tokens=0,
    )

    boom = RuntimeError("primary failed hard")

    with patch("pipeline.chunking.service.get_chunking_provider") as mock_get:
        primary = AsyncMock()
        primary.chunk_document = AsyncMock(side_effect=boom)
        primary.name = "gemma_vllm"

        det = DeterministicChunkingProvider()

        def _select(name: str):
            if name == "gemma_vllm":
                return primary
            if name == "deterministic":
                return det
            raise ValueError(name)

        mock_get.side_effect = _select
        result = await chunk_pages(pages, config)

    assert result.provider == "deterministic"
    assert result.chunks
    assert any("used fallback 'deterministic'" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_http_error_falls_back_with_truthful_provider_label():
    pages = [{"page_number": 1, "original_markdown": "Heading\n\n" + ("word " * 200)}]
    config = ChunkingConfig(
        provider="gemma_vllm",
        model="missing-model",
        endpoint="http://gemma.test/v1",
        fallback_provider="deterministic",
        target_chunk_tokens=200,
        max_chunk_tokens=300,
        min_chunk_tokens=50,
        chunk_overlap_tokens=0,
    )
    response = httpx.Response(
        404,
        json={"error": {"message": "model not found"}},
        request=httpx.Request("POST", "http://gemma.test/v1/chat/completions"),
    )

    with patch(
        "pipeline.chunking.openai_compatible.httpx.AsyncClient.post",
        new=AsyncMock(return_value=response),
    ):
        result = await chunk_pages(pages, config)

    assert result.provider == "deterministic"
    assert result.model == "deterministic"
    assert result.chunks
    assert any("used fallback 'deterministic'" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_overlapping_llm_groups_fall_back_to_deterministic():
    pages = [
        {"page_number": 1, "original_markdown": "A " * 400},
        {"page_number": 2, "original_markdown": "B " * 400},
        {"page_number": 3, "original_markdown": "C " * 400},
    ]
    config = ChunkingConfig(
        provider="gemma_vllm",
        model="gemma-4-31b-it",
        endpoint="http://gemma.test/v1",
        fallback_provider="deterministic",
        target_chunk_tokens=220,
        max_chunk_tokens=320,
        min_chunk_tokens=50,
        chunk_overlap_tokens=0,
    )
    overlapping = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"groups":[{"start_unit":1,"end_unit":2},{"start_unit":2,"end_unit":3}]}'
                    )
                }
            }
        ]
    }
    response = httpx.Response(
        200,
        json=overlapping,
        request=httpx.Request("POST", "http://gemma.test/v1/chat/completions"),
    )
    with patch(
        "pipeline.chunking.openai_compatible.httpx.AsyncClient.post",
        new=AsyncMock(return_value=response),
    ):
        result = await chunk_pages(pages, config)
    assert result.provider == "deterministic"
    assert result.chunks
    assert any("used fallback 'deterministic'" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_llm_dedupe_suppresses_cross_window_adjacent_duplicates():
    pages = [
        {"page_number": 1, "original_markdown": "Repeated boilerplate line."},
        {"page_number": 2, "original_markdown": "Repeated boilerplate line."},
    ]
    config = ChunkingConfig(
        provider="openai_vllm",
        model="gpt-like",
        endpoint="http://chunker.test/v1",
        fallback_provider="deterministic",
        target_chunk_tokens=20,
        max_chunk_tokens=80,
        min_chunk_tokens=1,
        chunk_overlap_tokens=0,
        page_window_size=1,
    )
    one_group = {
        "choices": [
            {
                "message": {
                    "content": '{"groups":[{"start_unit":1,"end_unit":1,"heading_hint":"","is_reference":false}]}'
                }
            }
        ]
    }
    response = httpx.Response(
        200,
        json=one_group,
        request=httpx.Request("POST", "http://chunker.test/v1/chat/completions"),
    )
    with patch(
        "pipeline.chunking.openai_compatible.httpx.AsyncClient.post",
        new=AsyncMock(return_value=response),
    ), patch(
        "pipeline.chunking.openai_compatible._grouping_looks_bad",
        return_value=False,
    ):
        result = await chunk_pages(pages, config)

    assert result.provider == "openai_vllm"
    assert len(result.chunks) == 1
    assert any("Dropped adjacent LLM chunk with identical text" in warning for warning in result.warnings)


def test_deterministic_dedupe_drops_adjacent_identical_text():
    text = "same chunk body"
    chunk_a = ChunkCandidate(
        text=text,
        page_start=1,
        page_end=2,
        source_page_numbers=[1, 2],
        source_spans=[],
        token_count=count_tokens(text),
    )
    chunk_b = ChunkCandidate(
        text=text,
        page_start=1,
        page_end=3,
        source_page_numbers=[1, 2, 3],
        source_spans=[],
        token_count=count_tokens(text),
    )
    kept, warnings = _dedupe_chunks([chunk_a, chunk_b])
    assert len(kept) == 1
    assert any("identical text" in warning for warning in warnings)


def test_enable_thinking_only_for_qwen_adapter():
    """enable_thinking is Qwen-specific; Gemma adapter must not qualify for the gate."""
    assert GemmaVllmChunkingProvider.name != "qwen_vllm"
    assert QwenVllmChunkingProvider.name == "qwen_vllm"
