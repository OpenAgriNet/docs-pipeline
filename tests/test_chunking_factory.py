"""Unit tests for chunking factory, adapters, and config builder."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pipeline.chunking.adapters import (
    GemmaVllmChunkingProvider,
    MistralVllmChunkingProvider,
    OpenaiVllmChunkingProvider,
    QwenVllmChunkingProvider,
)
from pipeline.chunking.base import ChunkingConfig
from pipeline.chunking.config_builder import (
    DEFAULT_CHUNKING_MODEL,
    DEFAULT_CHUNKING_PROVIDER,
    ChunkingConfigBuilder,
)
from pipeline.chunking.deterministic import DeterministicChunkingProvider
from pipeline.chunking.factory import get_chunking_provider, is_llm_grouping_provider, list_chunking_providers
from pipeline.chunking.service import chunk_pages, load_chunking_config


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


def test_builder_defaults_to_gemma(monkeypatch):
    monkeypatch.delenv("CHUNKING_PROVIDER", raising=False)
    monkeypatch.delenv("CHUNKING_MODEL", raising=False)
    monkeypatch.delenv("CHUNKING_VLLM_BASE_URL", raising=False)
    monkeypatch.delenv("CHUNKING_PAGE_WINDOW_SIZE", raising=False)
    monkeypatch.delenv("CHUNKING_OVERLAP_TOKENS", raising=False)
    cfg = ChunkingConfigBuilder.from_env().build()
    assert cfg.provider == DEFAULT_CHUNKING_PROVIDER == "gemma_vllm"
    assert cfg.model == DEFAULT_CHUNKING_MODEL == "gemma-4-31b-it"
    assert cfg.page_window_size == 4  # LLM default
    assert cfg.chunk_overlap_tokens <= 64


def test_builder_deterministic_keeps_overlap_defaults(monkeypatch):
    monkeypatch.delenv("CHUNKING_PAGE_WINDOW_SIZE", raising=False)
    monkeypatch.delenv("CHUNKING_OVERLAP_TOKENS", raising=False)
    cfg = (
        ChunkingConfigBuilder.from_env()
        .with_provider("deterministic")
        .with_model("deterministic")
        .with_chunk_overlap(128)
        .build()
    )
    assert cfg.provider == "deterministic"
    assert cfg.page_window_size == 8
    assert cfg.chunk_overlap_tokens == 128


def test_load_chunking_config_uses_builder_defaults(monkeypatch):
    monkeypatch.delenv("CHUNKING_PROVIDER", raising=False)
    monkeypatch.delenv("CHUNKING_MODEL", raising=False)
    cfg = load_chunking_config()
    assert cfg.provider == "gemma_vllm"
    assert cfg.model == "gemma-4-31b-it"


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
