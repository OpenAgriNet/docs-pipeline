"""Chunking service layer and provider selection."""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Optional

from .base import ChunkingConfig, ChunkingResult
from .deterministic import DeterministicChunkingProvider
from .recursive_splitter import RecursiveSplitterChunkingProvider


PROVIDERS = {
    "deterministic": DeterministicChunkingProvider,
    "recursive_splitter": RecursiveSplitterChunkingProvider,
}


def load_chunking_config(
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
) -> ChunkingConfig:
    provider = os.environ.get("CHUNKING_PROVIDER", "recursive_splitter").strip().lower()
    model = os.environ.get("CHUNKING_MODEL", provider or "recursive_splitter").strip() or provider
    target_chunk_tokens = int(os.environ.get("CHUNKING_TARGET_CHUNK_TOKENS", str(chunk_size)))
    max_chunk_tokens = int(os.environ.get("CHUNKING_MAX_CHUNK_TOKENS", str(chunk_size)))
    min_chunk_tokens = int(os.environ.get("CHUNKING_MIN_CHUNK_TOKENS", str(min_tokens)))
    chunk_overlap_tokens = int(os.environ.get("CHUNKING_OVERLAP_TOKENS", str(chunk_overlap)))
    max_pages_per_chunk = int(os.environ.get("CHUNKING_MAX_PAGES_PER_CHUNK", "8"))
    page_window_size = int(os.environ.get("CHUNKING_PAGE_WINDOW_SIZE", str(max_pages_per_chunk)))
    return ChunkingConfig(
        provider=provider,
        model=model,
        target_chunk_tokens=target_chunk_tokens,
        max_chunk_tokens=max_chunk_tokens,
        min_chunk_tokens=min_chunk_tokens,
        chunk_overlap_tokens=chunk_overlap_tokens,
        max_pages_per_chunk=max_pages_per_chunk,
        page_window_size=page_window_size,
        fallback_provider=os.environ.get("CHUNKING_FALLBACK_PROVIDER", "deterministic").strip().lower(),
        request_timeout_seconds=float(os.environ.get("CHUNKING_REQUEST_TIMEOUT_SECONDS", "120")),
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
