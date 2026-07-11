"""Chunking service layer and provider selection."""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Optional

from .base import ChunkingConfig, ChunkingResult
from .deterministic import DeterministicChunkingProvider
from .qwen_vllm import QwenVllmChunkingProvider


PROVIDERS = {
    "deterministic": DeterministicChunkingProvider,
    "qwen_vllm": QwenVllmChunkingProvider,
    "openai_vllm": QwenVllmChunkingProvider,
}


def load_chunking_config(
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
) -> ChunkingConfig:
    provider = os.environ.get("CHUNKING_PROVIDER", "deterministic").strip().lower()
    model = os.environ.get("CHUNKING_MODEL", provider or "deterministic").strip() or provider
    endpoint = os.environ.get("CHUNKING_VLLM_BASE_URL", "").strip()
    api_key = os.environ.get("CHUNKING_API_KEY", "").strip()
    llm_grouping_provider = provider in {"qwen_vllm", "openai_vllm"}
    default_target_chunk_tokens = max(chunk_size, 700) if llm_grouping_provider else chunk_size
    default_max_chunk_tokens = max(chunk_size, 900) if llm_grouping_provider else chunk_size
    default_min_chunk_tokens = max(min_tokens, 150) if llm_grouping_provider else min_tokens
    default_chunk_overlap_tokens = min(chunk_overlap, 64) if llm_grouping_provider else chunk_overlap
    target_chunk_tokens = int(os.environ.get("CHUNKING_TARGET_CHUNK_TOKENS", str(default_target_chunk_tokens)))
    max_chunk_tokens = int(os.environ.get("CHUNKING_MAX_CHUNK_TOKENS", str(default_max_chunk_tokens)))
    min_chunk_tokens = int(os.environ.get("CHUNKING_MIN_CHUNK_TOKENS", str(default_min_chunk_tokens)))
    chunk_overlap_tokens = int(os.environ.get("CHUNKING_OVERLAP_TOKENS", str(default_chunk_overlap_tokens)))
    max_pages_per_chunk = int(os.environ.get("CHUNKING_MAX_PAGES_PER_CHUNK", "8"))
    default_page_window_size = 4 if llm_grouping_provider else max_pages_per_chunk
    page_window_size = int(os.environ.get("CHUNKING_PAGE_WINDOW_SIZE", str(default_page_window_size)))
    return ChunkingConfig(
        provider=provider,
        model=model,
        endpoint=endpoint,
        api_key=api_key,
        target_chunk_tokens=target_chunk_tokens,
        max_chunk_tokens=max_chunk_tokens,
        min_chunk_tokens=min_chunk_tokens,
        chunk_overlap_tokens=chunk_overlap_tokens,
        max_pages_per_chunk=max_pages_per_chunk,
        page_window_size=page_window_size,
        temperature=float(os.environ.get("CHUNKING_TEMPERATURE", "0.0")),
        seed=int(os.environ.get("CHUNKING_SEED", "0")),
        fallback_provider=os.environ.get("CHUNKING_FALLBACK_PROVIDER", "deterministic").strip().lower(),
        request_timeout_seconds=float(os.environ.get("CHUNKING_REQUEST_TIMEOUT_SECONDS", "120")),
        qwen_enable_thinking=os.environ.get("CHUNKING_QWEN_ENABLE_THINKING", "false").strip().lower() == "true",
    )


async def chunk_pages(
    pages: list[dict],
    config: ChunkingConfig,
    progress_callback: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
) -> ChunkingResult:
    provider_cls = PROVIDERS.get(config.provider)
    if not provider_cls:
        raise ValueError(f"Unsupported chunking provider '{config.provider}'")

    provider = provider_cls()
    try:
        return await provider.chunk_document(pages, config, progress_callback=progress_callback)
    except Exception:
        if config.provider == config.fallback_provider or config.fallback_provider not in PROVIDERS:
            raise
        fallback = PROVIDERS[config.fallback_provider]()
        fallback_config = ChunkingConfig(**{**config.__dict__, "provider": config.fallback_provider, "model": config.fallback_provider})
        result = await fallback.chunk_document(pages, fallback_config, progress_callback=progress_callback)
        result.warnings.append(
            f"Primary chunking provider '{config.provider}' failed; used fallback '{config.fallback_provider}'"
        )
        return result
