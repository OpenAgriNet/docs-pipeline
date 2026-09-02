"""OpenAI-compatible vLLM chunking engine (shared by Gemma / Qwen / Mistral adapters)."""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Optional

import httpx

from .base import ChunkCandidate, ChunkingConfig, ChunkingProvider, ChunkingResult
from .page_units import best_page_text, is_reference_section, merge_units, normalize_text, split_page_into_units

GROUPING_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_unit": {"type": "integer"},
                    "end_unit": {"type": "integer"},
                    "heading_hint": {"type": "string"},
                    "is_reference": {"type": "boolean"},
                },
                "required": ["start_unit", "end_unit"],
            },
        }
    },
    "required": ["groups"],
}


def _extract_json_block(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not match:
        raise ValueError("Chunker response did not contain JSON")
    json_text = match.group(0)
    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        repaired = re.sub(r",\s*([}\]])", r"\1", json_text)
        repaired = re.sub(r"}\s*{", "}, {", repaired)
        repaired = re.sub(r'("|\d|true|false|null)\s*(")', r"\1, \2", repaired)
        repaired = re.sub(r'("|\d|true|false|null)\s*([{[])', r"\1, \2", repaired)
        repaired = re.sub(r'([}\]])\s*(")', r"\1, \2", repaired)
        repaired = re.sub(r'([}\]])\s*([{[])', r"\1, \2", repaired)
        return json.loads(repaired)


def _sanitize_heading_hint(raw_title: str, text: str) -> str:
    candidate = (raw_title or "").strip()[:200]
    if not candidate:
        return ""
    normalized_title = normalize_text(candidate)
    normalized_text = normalize_text(text)
    if len(normalized_title) < 4:
        return ""
    if normalized_text and normalized_title == normalized_text[: len(normalized_title)] and len(normalized_title) > 80:
        return ""
    return candidate


def _grouping_looks_bad(chunks: list[ChunkCandidate], unit_count: int, config: ChunkingConfig) -> bool:
    if not chunks:
        return True
    total_tokens = sum(chunk.token_count for chunk in chunks)
    avg_tokens = total_tokens / len(chunks)
    if avg_tokens < max(140, config.target_chunk_tokens * 0.4):
        return True
    if len(chunks) > max(3, unit_count):
        return True
    return False


def _dedupe_adjacent_chunks(chunks: list[ChunkCandidate]) -> tuple[list[ChunkCandidate], list[str]]:
    if not chunks:
        return [], []
    kept = [chunks[0]]
    warnings: list[str] = []
    for chunk in chunks[1:]:
        prev = kept[-1]
        if (
            normalize_text(chunk.text) == normalize_text(prev.text)
            and chunk.page_start <= prev.page_end + 1
        ):
            warnings.append(
                "Dropped adjacent LLM chunk with identical text on pages "
                f"{chunk.page_start}-{chunk.page_end}"
            )
            continue
        kept.append(chunk)
    return kept, warnings


def _append_with_adjacent_dedupe(
    accepted: list[ChunkCandidate], candidates: list[ChunkCandidate]
) -> list[str]:
    """Append candidates while deduping against the last accepted chunk.

    This keeps one rolling guard across page windows so duplicates straddling
    window boundaries are also suppressed.
    """
    warnings: list[str] = []
    for chunk in candidates:
        prev = accepted[-1] if accepted else None
        if (
            prev is not None
            and normalize_text(chunk.text) == normalize_text(prev.text)
            and chunk.page_start <= prev.page_end + 1
        ):
            warnings.append(
                "Dropped adjacent LLM chunk with identical text on pages "
                f"{chunk.page_start}-{chunk.page_end}"
            )
            continue
        accepted.append(chunk)
    return warnings


def _build_chat_payload(config: ChunkingConfig, prompt: str, provider_name: str) -> dict[str, Any]:
    """Build a raw OpenAI-compatible request body.

    ``extra_body`` is an OpenAI Python client argument, not a field accepted by
    the vLLM HTTP API. Use the standard JSON-schema response format directly so
    the payload works with both vLLM and other compatible servers.
    """
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": config.temperature,
        "max_tokens": 1800,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "chunk_groups",
                "schema": GROUPING_JSON_SCHEMA,
            },
        },
    }
    if provider_name == "qwen_vllm" and config.qwen_enable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": True}
    return payload


class OpenAiCompatibleChunkingProvider(ChunkingProvider):
    """Shared OpenAI-compatible chat/completions chunk boundary engine."""

    name = "openai_vllm"

    async def chunk_document(
        self,
        pages: list[dict],
        config: ChunkingConfig,
        progress_callback: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
    ) -> ChunkingResult:
        if not config.endpoint:
            raise ValueError(f"CHUNKING_VLLM_BASE_URL is required for {self.name} chunking")

        endpoint = config.endpoint.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"

        chunks: list[ChunkCandidate] = []
        warnings: list[str] = []
        page_window_size = max(1, config.page_window_size)
        total_pages = max(1, len(pages))
        total_windows = max(1, (len(pages) + page_window_size - 1) // page_window_size)
        windows_done = 0

        if progress_callback:
            await progress_callback(
                {
                    "provider": self.name,
                    "windows_processed": 0,
                    "windows_total": total_windows,
                    "pages_processed": 0,
                    "pages_total": total_pages,
                    "chunks_emitted": 0,
                    "percent": 0.0,
                }
            )

        async with httpx.AsyncClient(timeout=config.request_timeout_seconds) as client:
            for window_start in range(0, len(pages), page_window_size):
                window = pages[window_start : window_start + page_window_size]
                page_range = f"{window[0].get('page_number', 1)}-{window[-1].get('page_number', 1)}"
                window_emitted_chunks: list[ChunkCandidate] = []
                window_succeeded = False
                if progress_callback:
                    await progress_callback(
                        {
                            "provider": self.name,
                            "windows_processed": windows_done,
                            "windows_total": total_windows,
                            "pages_processed": window_start,
                            "pages_total": total_pages,
                            "chunks_emitted": len(chunks),
                            "percent": (windows_done / total_windows * 100.0) if total_windows else 0.0,
                        }
                    )
                try:
                    units: list[dict] = []
                    for page in window:
                        units.extend(split_page_into_units(page.get("page_number", 1), best_page_text(page), config))

                    if not units:
                        # Empty-unit windows still advance checkpoint cursors.
                        window_succeeded = True
                        continue

                    unit_payload = [
                        {
                            "unit_id": idx + 1,
                            "page_number": unit["page_number"],
                            "token_count": unit["token_count"],
                            "hint": unit.get("section_title") or "",
                            "text": unit["text"][:1200],
                        }
                        for idx, unit in enumerate(units)
                    ]

                    prompt = (
                        "You are a document chunk-boundary engine. Return JSON only.\n"
                        "The input is a sequence of numbered text units extracted from consecutive pages.\n"
                        "Your job is to group adjacent unit IDs into semantically coherent retrieval chunks.\n"
                        "Do not rewrite the unit text. Do not return chunk text. Return only grouping decisions.\n"
                        "Prefer fewer, larger chunks with strong semantic cohesion.\n"
                        "Keep headings with the content they introduce. Avoid isolated micro-chunks.\n"
                        "Group only adjacent units. Do not skip or reorder unit IDs.\n"
                        "Return this exact shape: "
                        '{"groups": [{"start_unit": int, "end_unit": int, "heading_hint": str, "is_reference": bool}]}\n\n'
                        f"Target chunk tokens: {config.target_chunk_tokens}\n"
                        f"Max chunk tokens: {config.max_chunk_tokens}\n"
                        f"Min chunk tokens: {config.min_chunk_tokens}\n"
                        f"Units:\n{json.dumps(unit_payload, ensure_ascii=False)}"
                    )

                    payload = _build_chat_payload(config, prompt, self.name)

                    response = await client.post(endpoint, headers=headers, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    message = data["choices"][0]["message"]
                    content = message.get("content")
                    if content is None:
                        content = message.get("reasoning") or ""
                    parsed = _extract_json_block(content)
                    groups = parsed.get("groups", [])
                    window_candidates: list[ChunkCandidate] = []
                    covered_units: set[int] = set()

                    for group in groups:
                        start_unit = int(group.get("start_unit"))
                        end_unit = int(group.get("end_unit"))
                        if start_unit < 1 or end_unit < start_unit or end_unit > len(units):
                            raise ValueError(f"Invalid unit span {start_unit}-{end_unit}")
                        overlap = covered_units.intersection(range(start_unit, end_unit + 1))
                        if overlap:
                            overlap_preview = sorted(overlap)[:10]
                            raise ValueError(
                                f"{self.name} emitted overlapping unit spans on pages {page_range}: "
                                f"{start_unit}-{end_unit} overlaps {overlap_preview}"
                            )
                        group_units = units[start_unit - 1 : end_unit]
                        candidate = merge_units(
                            group_units,
                            section_title_hint=_sanitize_heading_hint(
                                group.get("heading_hint") or "", group_units[0]["text"]
                            ),
                            is_reference_hint=bool(group.get("is_reference"))
                            or is_reference_section("\n\n".join(unit["text"] for unit in group_units)),
                        )
                        candidate.metadata["unit_span"] = {"start_unit": start_unit, "end_unit": end_unit}
                        window_candidates.append(candidate)
                        covered_units.update(range(start_unit, end_unit + 1))

                    missing_units = [idx for idx in range(1, len(units) + 1) if idx not in covered_units]
                    if missing_units:
                        raise ValueError(f"Chunker left units uncovered: {missing_units[:12]}")

                    window_candidates, dedupe_warnings = _dedupe_adjacent_chunks(window_candidates)
                    warnings.extend(dedupe_warnings)

                    if _grouping_looks_bad(window_candidates, len(units), config):
                        raise ValueError(
                            f"{self.name} grouping looked fragmented for pages {page_range}"
                        )
                    before_count = len(chunks)
                    warnings.extend(_append_with_adjacent_dedupe(chunks, window_candidates))
                    window_emitted_chunks = list(chunks[before_count:])
                    window_succeeded = True
                finally:
                    windows_done += 1
                    if progress_callback:
                        pages_processed = min(total_pages, window_start + len(window))
                        percent = windows_done / total_windows * 100.0
                        checkpoint_window_chunks = [
                            {
                                "text": c.text,
                                "page_start": c.page_start,
                                "page_end": c.page_end,
                                "source_page_numbers": c.source_page_numbers,
                                "source_spans": c.source_spans,
                                "token_count": c.token_count,
                                "section_title": c.section_title,
                                "content_type": c.content_type,
                                "is_reference": c.is_reference,
                            }
                            for c in window_emitted_chunks
                        ]
                        await progress_callback(
                            {
                                "provider": self.name,
                                "windows_processed": windows_done,
                                "windows_total": total_windows,
                                "pages_processed": pages_processed,
                                "pages_total": total_pages,
                                "chunks_emitted": len(chunks),
                                "percent": percent,
                                "window_succeeded": window_succeeded,
                                "checkpoint_window_chunks": checkpoint_window_chunks,
                            }
                        )

        return ChunkingResult(
            chunks=chunks,
            provider=self.name,
            model=config.model,
            config=config.__class__(**{**config.__dict__, "provider": self.name}),
            warnings=warnings,
            stats={"page_count": len(pages), "chunk_count": len(chunks)},
        )
