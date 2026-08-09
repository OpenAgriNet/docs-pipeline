"""Chunking service layer and provider selection."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from .base import ChunkingConfig, ChunkingResult
from .config_builder import ChunkingConfigBuilder
from .factory import PROVIDER_REGISTRY, get_chunking_provider


def load_chunking_config(
    chunk_size: int = 450,
    chunk_overlap: int = 128,
    min_tokens: int = 100,
) -> ChunkingConfig:
    return (
        ChunkingConfigBuilder.from_env()
        .with_chunk_size(chunk_size)
        .with_chunk_overlap(chunk_overlap)
        .with_min_tokens(min_tokens)
        .build()
    )


async def chunk_pages(
    pages: list[dict],
    config: ChunkingConfig,
    progress_callback: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
) -> ChunkingResult:
    provider = get_chunking_provider(config.provider)
    try:
        return await provider.chunk_document(pages, config, progress_callback=progress_callback)
    except Exception:
        if config.provider == config.fallback_provider:
            raise
        try:
            fallback = get_chunking_provider(config.fallback_provider)
        except ValueError:
            raise
        fallback_config = ChunkingConfig(
            **{**config.__dict__, "provider": config.fallback_provider, "model": config.fallback_provider}
        )
        result = await fallback.chunk_document(pages, fallback_config, progress_callback=progress_callback)
        result.warnings.append(
            f"Primary chunking provider '{config.provider}' failed; used fallback '{config.fallback_provider}'"
        )
        return result


# Back-compat for anything that imported PROVIDERS from this module.
PROVIDERS = PROVIDER_REGISTRY
