"""Builder for ChunkingConfig (creational Builder pattern)."""

from __future__ import annotations

import os
from typing import Optional

from .base import ChunkingConfig
from .factory import PROVIDER_REGISTRY, is_llm_grouping_provider

DEFAULT_FALLBACK_PROVIDER = "deterministic"


class ChunkingConfigBuilder:
    """Fluent builder for assembling ChunkingConfig from env + overrides."""

    def __init__(self) -> None:
        self._provider: str = ""
        self._model: str = ""
        self._endpoint: str = ""
        self._api_key: str = ""
        self._chunk_size: int = 450
        self._chunk_overlap: int = 128
        self._min_tokens: int = 100
        self._target_chunk_tokens: Optional[int] = None
        self._max_chunk_tokens: Optional[int] = None
        self._min_chunk_tokens: Optional[int] = None
        self._chunk_overlap_tokens: Optional[int] = None
        self._max_pages_per_chunk: Optional[int] = None
        self._page_window_size: Optional[int] = None
        self._temperature: Optional[float] = None
        self._seed: Optional[int] = None
        self._fallback_provider: str = DEFAULT_FALLBACK_PROVIDER
        self._request_timeout_seconds: Optional[float] = None
        self._qwen_enable_thinking: Optional[bool] = None

    @classmethod
    def from_env(cls) -> "ChunkingConfigBuilder":
        builder = cls()
        return builder.with_env()

    def with_env(self) -> "ChunkingConfigBuilder":
        provider = os.environ.get("CHUNKING_PROVIDER", "").strip().lower()
        if not provider:
            supported = ", ".join(sorted(PROVIDER_REGISTRY))
            raise ValueError(
                "CHUNKING_PROVIDER is required (no app-wide vendor default). "
                f"Set it explicitly. Supported: {supported}"
            )
        if provider not in PROVIDER_REGISTRY:
            supported = ", ".join(sorted(PROVIDER_REGISTRY))
            raise ValueError(
                f"Unsupported CHUNKING_PROVIDER '{provider}'. Supported: {supported}"
            )
        self._provider = provider

        model = os.environ.get("CHUNKING_MODEL", "").strip()
        if not model:
            if is_llm_grouping_provider(provider):
                raise ValueError(
                    "CHUNKING_MODEL is required when CHUNKING_PROVIDER is an LLM "
                    f"provider ({provider}). Set it to the exact model id served by the endpoint."
                )
            model = provider
        self._model = model

        self._endpoint = os.environ.get("CHUNKING_VLLM_BASE_URL", "").strip()
        self._api_key = os.environ.get("CHUNKING_API_KEY", "").strip()
        self._fallback_provider = (
            os.environ.get("CHUNKING_FALLBACK_PROVIDER", DEFAULT_FALLBACK_PROVIDER).strip().lower()
            or DEFAULT_FALLBACK_PROVIDER
        )
        if self._fallback_provider not in PROVIDER_REGISTRY:
            supported = ", ".join(sorted(PROVIDER_REGISTRY))
            raise ValueError(
                f"Unsupported CHUNKING_FALLBACK_PROVIDER '{self._fallback_provider}'. "
                f"Supported: {supported}"
            )
        if "CHUNKING_TARGET_CHUNK_TOKENS" in os.environ:
            self._target_chunk_tokens = int(os.environ["CHUNKING_TARGET_CHUNK_TOKENS"])
        if "CHUNKING_MAX_CHUNK_TOKENS" in os.environ:
            self._max_chunk_tokens = int(os.environ["CHUNKING_MAX_CHUNK_TOKENS"])
        if "CHUNKING_MIN_CHUNK_TOKENS" in os.environ:
            self._min_chunk_tokens = int(os.environ["CHUNKING_MIN_CHUNK_TOKENS"])
        if "CHUNKING_OVERLAP_TOKENS" in os.environ:
            self._chunk_overlap_tokens = int(os.environ["CHUNKING_OVERLAP_TOKENS"])
        if "CHUNKING_MAX_PAGES_PER_CHUNK" in os.environ:
            self._max_pages_per_chunk = int(os.environ["CHUNKING_MAX_PAGES_PER_CHUNK"])
        if "CHUNKING_PAGE_WINDOW_SIZE" in os.environ:
            self._page_window_size = int(os.environ["CHUNKING_PAGE_WINDOW_SIZE"])
        if "CHUNKING_TEMPERATURE" in os.environ:
            self._temperature = float(os.environ["CHUNKING_TEMPERATURE"])
        if "CHUNKING_SEED" in os.environ:
            self._seed = int(os.environ["CHUNKING_SEED"])
        if "CHUNKING_REQUEST_TIMEOUT_SECONDS" in os.environ:
            self._request_timeout_seconds = float(os.environ["CHUNKING_REQUEST_TIMEOUT_SECONDS"])
        if "CHUNKING_QWEN_ENABLE_THINKING" in os.environ:
            self._qwen_enable_thinking = (
                os.environ.get("CHUNKING_QWEN_ENABLE_THINKING", "false").strip().lower() == "true"
            )
        return self

    def with_provider(self, provider: str) -> "ChunkingConfigBuilder":
        self._provider = (provider or "").strip().lower()
        return self

    def with_model(self, model: str) -> "ChunkingConfigBuilder":
        self._model = (model or "").strip()
        return self

    def with_endpoint(self, endpoint: str) -> "ChunkingConfigBuilder":
        self._endpoint = (endpoint or "").strip()
        return self

    def with_chunk_size(self, chunk_size: int) -> "ChunkingConfigBuilder":
        self._chunk_size = int(chunk_size)
        return self

    def with_chunk_overlap(self, chunk_overlap: int) -> "ChunkingConfigBuilder":
        self._chunk_overlap = int(chunk_overlap)
        return self

    def with_min_tokens(self, min_tokens: int) -> "ChunkingConfigBuilder":
        self._min_tokens = int(min_tokens)
        return self

    def with_fallback_provider(self, provider: str) -> "ChunkingConfigBuilder":
        self._fallback_provider = (provider or DEFAULT_FALLBACK_PROVIDER).strip().lower()
        return self

    def build(self) -> ChunkingConfig:
        if not self._provider:
            raise ValueError(
                "Chunking provider is required. Call with_env() or with_provider(...) before build()."
            )
        if self._provider not in PROVIDER_REGISTRY:
            supported = ", ".join(sorted(PROVIDER_REGISTRY))
            raise ValueError(
                f"Unsupported chunking provider '{self._provider}'. Supported: {supported}"
            )

        llm = is_llm_grouping_provider(self._provider)
        default_target = max(self._chunk_size, 700) if llm else self._chunk_size
        default_max = max(self._chunk_size, 900) if llm else self._chunk_size
        default_min = max(self._min_tokens, 150) if llm else self._min_tokens
        default_overlap = min(self._chunk_overlap, 64) if llm else self._chunk_overlap
        max_pages = self._max_pages_per_chunk if self._max_pages_per_chunk is not None else 8
        default_window = 4 if llm else max_pages

        model = self._model
        if not model:
            if llm:
                raise ValueError(
                    f"CHUNKING_MODEL is required for LLM provider '{self._provider}'."
                )
            model = self._provider

        return ChunkingConfig(
            provider=self._provider,
            model=model,
            endpoint=self._endpoint,
            api_key=self._api_key,
            target_chunk_tokens=self._target_chunk_tokens if self._target_chunk_tokens is not None else default_target,
            max_chunk_tokens=self._max_chunk_tokens if self._max_chunk_tokens is not None else default_max,
            min_chunk_tokens=self._min_chunk_tokens if self._min_chunk_tokens is not None else default_min,
            chunk_overlap_tokens=self._chunk_overlap_tokens if self._chunk_overlap_tokens is not None else default_overlap,
            max_pages_per_chunk=max_pages,
            page_window_size=self._page_window_size if self._page_window_size is not None else default_window,
            temperature=self._temperature if self._temperature is not None else 0.0,
            seed=self._seed if self._seed is not None else 0,
            fallback_provider=self._fallback_provider,
            request_timeout_seconds=(
                self._request_timeout_seconds if self._request_timeout_seconds is not None else 120.0
            ),
            qwen_enable_thinking=bool(self._qwen_enable_thinking) if self._qwen_enable_thinking is not None else False,
        )
