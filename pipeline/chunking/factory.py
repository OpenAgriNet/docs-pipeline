"""Chunking provider factory."""

from __future__ import annotations

from .adapters import (
    DeterministicChunkingAdapter,
    GemmaVllmChunkingProvider,
    MistralVllmChunkingProvider,
    OpenaiVllmChunkingProvider,
    QwenVllmChunkingProvider,
)
from .base import ChunkingProvider

PROVIDER_REGISTRY: dict[str, type[ChunkingProvider]] = {
    "deterministic": DeterministicChunkingAdapter,
    "gemma_vllm": GemmaVllmChunkingProvider,
    "qwen_vllm": QwenVllmChunkingProvider,
    "mistral_vllm": MistralVllmChunkingProvider,
    "openai_vllm": OpenaiVllmChunkingProvider,
}


def list_chunking_providers() -> list[str]:
    return sorted(PROVIDER_REGISTRY)


def get_chunking_provider(provider: str) -> ChunkingProvider:
    key = (provider or "").strip().lower()
    provider_cls = PROVIDER_REGISTRY.get(key)
    if not provider_cls:
        supported = ", ".join(list_chunking_providers())
        raise ValueError(f"Unsupported chunking provider '{provider}'. Supported: {supported}")
    return provider_cls()


def is_llm_grouping_provider(provider: str) -> bool:
    key = (provider or "").strip().lower()
    return key in {"gemma_vllm", "qwen_vllm", "mistral_vllm", "openai_vllm"}
