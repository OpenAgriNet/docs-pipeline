"""Malformed windows must recover without replaying successful checkpoint windows."""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from pipeline.chunking.base import ChunkingConfig, count_tokens
from pipeline.chunking.openai_compatible import _fallback_groups, _validated_groups
from pipeline.chunking.service import chunk_pages


def config(**changes):
    return ChunkingConfig(**{
        "provider": "gemma_vllm", "model": "gemma", "endpoint": "http://chunker.test/v1",
        "fallback_provider": "deterministic", "page_window_size": 1,
        "target_chunk_tokens": 450, "max_chunk_tokens": 600, "min_chunk_tokens": 1,
        "chunk_overlap_tokens": 0, **changes,
    })


def response(groups):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"groups": groups})}}]},
                          request=httpx.Request("POST", "http://chunker.test/v1/chat/completions"))


GOOD = [{"start_unit": 1, "end_unit": 1}]
BAD = [{"start_unit": 1, "end_unit": 1}, {"start_unit": 1, "end_unit": 1}]


@pytest.mark.parametrize("second_reply, fallback", [(GOOD, False), (BAD, True)])
def test_retry_and_fallback_only_emit_one_successful_checkpoint(second_reply, fallback):
    pages = [{"page_number": n, "original_markdown": f"UniquePage{n} " * 100} for n in (1, 2, 3)]
    events = []
    async def progress(event):
        events.append(event)
    replies = [response(GOOD), response(BAD), response(second_reply), response(GOOD)]
    post = AsyncMock(side_effect=replies)
    with patch("pipeline.chunking.openai_compatible.httpx.AsyncClient.post", post):
        result = asyncio.run(chunk_pages(pages, config(), progress))
    assert post.await_count == 4  # The valid first/third windows were never retried.
    assert "Previous response was invalid" in post.call_args_list[2].kwargs["json"]["messages"][0]["content"]
    accepted = [e for e in events if e.get("window_succeeded")]
    assert [e["pages_processed"] for e in accepted] == [1, 2, 3]
    assert [e["pages_total"] for e in accepted] == [3, 3, 3]
    for n in (1, 2, 3):
        assert sum(c.text.count(f"UniquePage{n}") for c in result.chunks) == 100
    assert any("used fallback" in w for w in result.warnings) == fallback
    assert result.stats["fallback_windows"] == int(fallback)


def test_strict_config_does_not_silently_enable_fallback():
    post = AsyncMock(return_value=response(BAD))
    events = []
    async def progress(event): events.append(event)
    with patch("pipeline.chunking.openai_compatible.httpx.AsyncClient.post", post):
        with pytest.raises(ValueError, match="pages 73-73.*overlapping"):
            asyncio.run(chunk_pages([{"page_number": 73, "original_markdown": "cattle " * 200}],
                                    config(fallback_provider="gemma_vllm"), progress))
    assert post.await_count == 2
    assert not any(e.get("window_succeeded") for e in events)


@pytest.mark.parametrize("groups", [
    [{"start_unit": 2, "end_unit": 2}, {"start_unit": 1, "end_unit": 1}],
    [{"start_unit": 1, "end_unit": 1}],
    [{"start_unit": 1, "end_unit": 2}, {"start_unit": 2, "end_unit": 2}],
])
def test_reject_reordering_gaps_and_overlaps(groups):
    units = [{"text": f"unit {n}", "page_number": 1, "token_count": 2,
              "source_spans": [{"page_number": 1, "start_char": n * 10, "end_char": n * 10 + 6}]} for n in (1, 2)]
    with pytest.raises(ValueError):
        _validated_groups(json.dumps({"groups": groups}), units, "1-1", "gemma_vllm")


def test_fallback_covers_every_ordered_unit_including_short_tail():
    units = [{"text": f"Unit{n} " + "cattle " * 40, "page_number": 25 + n // 2,
              "source_spans": [{"page_number": 25 + n // 2, "start_char": n * 300, "end_char": n * 300 + 260}],
              "token_count": count_tokens(f"Unit{n} " + "cattle " * 40)} for n in range(9)]
    chunks = _fallback_groups(units, config(target_chunk_tokens=100))
    combined = "\n".join(c.text for c in chunks)
    assert [combined.index(f"Unit{n} ") for n in range(9)] == sorted(combined.index(f"Unit{n} ") for n in range(9))
    assert all(combined.count(f"Unit{n} ") == 1 for n in range(9))
    assert all(c.metadata["fallback_provider"] == "deterministic" for c in chunks)


def test_checkpoint_callback_failure_never_replays_document_via_global_fallback():
    events = []
    async def progress(event):
        if event.get("window_succeeded"):
            events.append(event)
            raise RuntimeError("checkpoint disk write failed")
    post = AsyncMock(return_value=response(GOOD))
    with patch("pipeline.chunking.openai_compatible.httpx.AsyncClient.post", post):
        with pytest.raises(RuntimeError, match="checkpoint disk write failed"):
            asyncio.run(chunk_pages([{"page_number": 1, "original_markdown": "UniquePage1 " * 100}], config(), progress))
    assert len(events) == 1
    assert post.await_count == 1
