"""Backward-compatible re-export of the Qwen vLLM chunking adapter."""

from .adapters import QwenVllmChunkingProvider
from .openai_compatible import (
    GROUPING_JSON_SCHEMA,
    OpenAiCompatibleChunkingProvider,
    _extract_json_block,
    _grouping_looks_bad,
    _sanitize_heading_hint,
)

__all__ = [
    "GROUPING_JSON_SCHEMA",
    "OpenAiCompatibleChunkingProvider",
    "QwenVllmChunkingProvider",
    "_extract_json_block",
    "_grouping_looks_bad",
    "_sanitize_heading_hint",
]
