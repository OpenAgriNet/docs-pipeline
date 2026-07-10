"""Auto domain tagging via Gemma vLLM (OpenAI-compatible chat API)."""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

import httpx

from .base import DomainTag, flatten_taxonomy_values, load_taxonomy, normalize_tag_key

TAG_PROMPT = """You label veterinary and dairy extension content with domain tags.

Return ONLY valid JSON (no markdown fences):
{{"tags": ["dimension:value", ...]}}

Rules:
- Use only tags from the allowed vocabulary below.
- Pick 2-8 tags that best describe the chunk.
- Use flat dimension:value strings (example: region:north, topic:nutrition/feed).
- Overlapping tags are allowed.
- If unsure, prefer broader topic/claim tags over guessing breed/condition.

Document filename: {filename}
Document context: {doc_context}

Allowed vocabulary (dimension: allowed values):
{allowed_vocab}

Chunk text:
{text}
"""


class GemmaDomainTagger:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str = "",
        request_timeout_seconds: float = 120.0,
        max_output_tokens: int = 1024,
    ):
        if not endpoint:
            raise ValueError("DOMAIN_TAGGING_VLLM_BASE_URL or TRANSLATION_VLLM_BASE_URL is required")
        self._endpoint = endpoint.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key
        self.request_timeout_seconds = request_timeout_seconds
        self.max_output_tokens = max_output_tokens

    def suggest_tags(
        self,
        text: str,
        *,
        filename: str = "",
        doc_context: str = "",
        taxonomy: dict | None = None,
    ) -> list[DomainTag]:
        taxonomy = taxonomy or load_taxonomy()
        allowed = flatten_taxonomy_values(taxonomy)
        vocab_lines = []
        for dimension in sorted(allowed):
            values = ", ".join(sorted(allowed[dimension]))
            vocab_lines.append(f"- {dimension}: {values}")
        prompt = TAG_PROMPT.format(
            filename=filename or "(unknown)",
            doc_context=doc_context or "(none)",
            allowed_vocab="\n".join(vocab_lines),
            text=(text or "")[:6000],
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": self.max_output_tokens,
        }

        with httpx.Client(timeout=self.request_timeout_seconds) as client:
            response = client.post(self._endpoint, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        content = ""
        choices = data.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""

        return _parse_tag_response(content, allowed)


def _parse_tag_response(content: str, allowed: dict[str, set[str]]) -> list[DomainTag]:
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    tags_raw: list[str] = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            raw_tags = parsed.get("tags") or parsed.get("domain_tags") or []
            if isinstance(raw_tags, list):
                tags_raw = [str(t) for t in raw_tags]
    except json.JSONDecodeError:
        tags_raw = re.findall(r"[a-z][a-z0-9_/\-]*:[a-z0-9_/\-]+", text, flags=re.IGNORECASE)

    result: list[DomainTag] = []
    seen: set[str] = set()
    for raw in tags_raw:
        key = normalize_tag_key(raw)
        if not key or key in seen:
            continue
        dimension, value = key.split(":", 1)
        allowed_values = allowed.get(dimension)
        if allowed_values and value not in allowed_values:
            continue
        result.append(DomainTag(dimension=dimension, value=value, source="auto"))
        seen.add(key)
    return result


async def auto_tag_chunks(
    chunks: list[dict],
    *,
    filename: str = "",
    doc_context: str = "",
    tagger: GemmaDomainTagger,
    log: Optional[Callable[..., None]] = None,
) -> dict[int, list[DomainTag]]:
    import asyncio

    tagged: dict[int, list[DomainTag]] = {}

    async def tag_one(chunk: dict) -> tuple[int, list[DomainTag]]:
        chunk_num = int(chunk.get("chunk_number") or 0)
        text = chunk.get("edited_text") or chunk.get("original_text") or ""
        if not text.strip():
            return chunk_num, []
        try:
            tags = await asyncio.to_thread(
                tagger.suggest_tags,
                text,
                filename=filename,
                doc_context=doc_context,
            )
            if log:
                log("Auto-tagged chunk %s with %s tags", chunk_num, len(tags))
            return chunk_num, tags
        except Exception as exc:
            if log:
                log("Auto-tag failed for chunk %s: %s", chunk_num, exc)
            return chunk_num, []

    results = await asyncio.gather(*[tag_one(chunk) for chunk in chunks])
    for chunk_num, tags in results:
        if chunk_num:
            tagged[chunk_num] = tags
    return tagged
