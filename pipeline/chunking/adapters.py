"""Named chunking adapters over shared engines (Adapter pattern)."""

from __future__ import annotations

from .deterministic import DeterministicChunkingProvider
from .openai_compatible import OpenAiCompatibleChunkingProvider


class GemmaVllmChunkingProvider(OpenAiCompatibleChunkingProvider):
    """Adapter: Gemma via OpenAI-compatible vLLM."""

    name = "gemma_vllm"


class QwenVllmChunkingProvider(OpenAiCompatibleChunkingProvider):
    """Adapter: Qwen via OpenAI-compatible vLLM."""

    name = "qwen_vllm"


class MistralVllmChunkingProvider(OpenAiCompatibleChunkingProvider):
    """Adapter: Mistral via OpenAI-compatible vLLM."""

    name = "mistral_vllm"


class OpenaiVllmChunkingProvider(OpenAiCompatibleChunkingProvider):
    """Generic OpenAI-compatible vLLM adapter (legacy alias id)."""

    name = "openai_vllm"


# Deterministic is already a ChunkingProvider; re-export for factory symmetry.
DeterministicChunkingAdapter = DeterministicChunkingProvider

__all__ = [
    "DeterministicChunkingAdapter",
    "GemmaVllmChunkingProvider",
    "MistralVllmChunkingProvider",
    "OpenaiVllmChunkingProvider",
    "QwenVllmChunkingProvider",
]
