"""Chunking provider interfaces and shared structures."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Optional

import tiktoken


def count_tokens(text: str, model: str = "cl100k_base") -> int:
    if not text:
        return 0
    encoder = tiktoken.get_encoding(model)
    return len(encoder.encode(str(text), disallowed_special=()))


@dataclass
class ChunkingConfig:
    provider: str
    model: str
    endpoint: str = ""
    api_key: str = ""
    target_chunk_tokens: int = 450
    max_chunk_tokens: int = 450
    min_chunk_tokens: int = 100
    chunk_overlap_tokens: int = 128
    max_pages_per_chunk: int = 8
    page_window_size: int = 8
    preserve_headings: bool = True
    preserve_tables: bool = True
    reference_policy: str = "detect"
    language_hint: str = ""
    temperature: float = 0.0
    seed: int = 0
    response_format_version: str = "v1"
    fallback_provider: str = "deterministic"
    request_timeout_seconds: float = 120.0
    qwen_enable_thinking: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass
class ChunkCandidate:
    text: str
    page_start: int
    page_end: int
    source_page_numbers: list[int]
    source_spans: list[dict[str, int]]
    token_count: int
    section_title: str = ""
    content_type: str = "body"
    is_reference: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkingResult:
    chunks: list[ChunkCandidate]
    provider: str
    model: str
    config: ChunkingConfig
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


class ChunkingProvider(ABC):
    name: str

    @abstractmethod
    async def chunk_document(
        self,
        pages: list[dict],
        config: ChunkingConfig,
        progress_callback: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
    ) -> ChunkingResult:
        raise NotImplementedError
