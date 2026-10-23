"""Streaming chunk persist + delayed Approve Chunks (#157)."""

from __future__ import annotations

import json

import pytest

import pipeline.temporal.document_tasks as activities
from pipeline.chunking.base import ChunkCandidate, ChunkingConfig, ChunkingResult
from pipeline.chunking.deterministic import DeterministicChunkingProvider
from pipeline.services.documents import list_available_actions
from tests.helpers import seed_document

pytestmark = pytest.mark.asyncio


def _window_chunk(text: str, page: int, token_count: int = 3) -> dict:
    return {
        "text": text, "page_start": page, "page_end": page, "source_page_numbers": [page], "source_spans": [],
        "token_count": token_count, "section_title": "", "content_type": "body", "is_reference": False,
    }


def _window(pages_processed: int, *chunks: dict) -> dict:
    """Provider progress event: one more window succeeded, emitting ``chunks``."""
    return {"pages_processed": pages_processed, "window_succeeded": True, "checkpoint_window_chunks": list(chunks)}


def _result(config, *chunks: ChunkCandidate) -> ChunkingResult:
    return ChunkingResult(
        chunks=list(chunks), provider=config.provider, model=config.model, config=config,
        stats={"chunk_count": len(chunks)},
    )


def _streaming(db, monkeypatch, workflow_id, pages, chunk_pages, *, prior_chunks=()):
    """Seed a pipeline run over ``pages`` and route chunking to ``chunk_pages``
    (deterministic, so never checkpointed: rows stream in as they are emitted)."""
    seed_document(
        db, workflow_id, stage="chunking", page_count=len(pages), chunk_count=len(prior_chunks),
        pages=pages, chunks=prior_chunks,
    )
    db.create_document_job(
        workflow_id=workflow_id, job_type="pipeline", status="running", current_stage="chunking",
        config={"source": "pipeline"},
    )
    monkeypatch.setenv("CHUNKING_CHECKPOINT_MIN_PAGES", "500")
    cfg = ChunkingConfig(provider="deterministic", model="deterministic", fallback_provider="deterministic")
    monkeypatch.setattr(activities, "load_chunking_config", lambda **_kwargs: cfg)
    monkeypatch.setattr(activities, "chunk_pages", chunk_pages)


def _texts(db, workflow_id):
    return [row["original_text"] for row in db.get_chunks(workflow_id, include_excluded=True)]


def _assert_still_chunking_without_approve(db, workflow_id):
    doc = db.get_document(workflow_id)
    assert doc["stage"] == "chunking"
    assert "approve_chunks" not in list_available_actions(doc, db.get_latest_document_job(workflow_id))


async def test_non_checkpoint_chunking_streams_rows_before_finalize(db_connection, monkeypatch, minio_uploads):
    mid_run: list[int] = []

    async def chunk_pages(pages, config, progress_callback=None):
        await progress_callback(_window(1, _window_chunk("streamed chunk one", 1)))
        mid_run.append(len(_texts(db_connection, "wf")))
        await progress_callback(_window(2, _window_chunk("streamed chunk two", 2)))
        return _result(
            config,
            ChunkCandidate("streamed chunk one", 1, 1, [1], [], 3),
            ChunkCandidate("streamed chunk two", 2, 2, [2], [], 3),
        )

    _streaming(db_connection, monkeypatch, "wf", ["page one content", "page two content"], chunk_pages)
    result = await activities.create_chunks_from_db("wf")
    assert mid_run == [1], "the first window's row must be visible before chunking finishes"
    assert result["chunk_count"] == 2
    assert _texts(db_connection, "wf") == ["streamed chunk one", "streamed chunk two"]
    doc = db_connection.get_document("wf")
    assert doc["stage"] == "chunking"
    assert int(doc["chunk_count"] or 0) == 2


async def test_stream_drops_adjacent_duplicate_text(db_connection, monkeypatch, minio_uploads):
    mid_run: list[int] = []

    async def chunk_pages(pages, config, progress_callback=None):
        await progress_callback(_window(2, _window_chunk("same text", 1), _window_chunk("same text", 2)))
        mid_run.append(len(_texts(db_connection, "wf")))
        return _result(config, ChunkCandidate("same text", 1, 2, [1, 2], [], 3))

    _streaming(db_connection, monkeypatch, "wf", ["same", "same"], chunk_pages)
    result = await activities.create_chunks_from_db("wf")
    assert mid_run == [1]
    assert result["chunk_count"] == 1
    assert _texts(db_connection, "wf") == ["same text"]


async def test_stream_persists_sparse_candidate_fields(db_connection, monkeypatch, minio_uploads):
    streamed_token_counts: list[int] = []

    async def chunk_pages(pages, config, progress_callback=None):
        await progress_callback(_window(1, {"text": "hello"}))
        streamed = db_connection.get_chunks("wf", include_excluded=True)
        streamed_token_counts.extend(int(row["token_count"] or 0) for row in streamed)
        return _result(config, ChunkCandidate("hello", 1, 1, [1], [], 1))

    _streaming(db_connection, monkeypatch, "wf", ["page"], chunk_pages)
    result = await activities.create_chunks_from_db("wf")
    assert streamed_token_counts == [0], "one streamed row, with the missing token count defaulted"
    assert result["chunk_count"] == 1
    row = db_connection.get_chunks("wf", include_excluded=True)[0]
    assert row["original_text"] == "hello"
    assert int(row["page_start"] or 0) == 1


async def test_stream_crash_keeps_partial_rows_and_blocks_approve(db_connection, monkeypatch, minio_uploads):
    async def chunk_pages(pages, config, progress_callback=None):
        await progress_callback(_window(1, _window_chunk("streamed one", 1)))
        raise RuntimeError("chunker crashed")

    _streaming(db_connection, monkeypatch, "wf", ["one", "two"], chunk_pages)
    with pytest.raises(RuntimeError, match="chunker crashed"):
        await activities.create_chunks_from_db("wf")

    assert _texts(db_connection, "wf") == ["streamed one"]
    _assert_still_chunking_without_approve(db_connection, "wf")
    db_connection.reconcile_materialized_state("wf")
    _assert_still_chunking_without_approve(db_connection, "wf")


async def test_non_checkpoint_start_clears_prior_chunks_before_provider_runs(db_connection, monkeypatch, minio_uploads):
    async def chunk_pages(pages, config, progress_callback=None):
        raise RuntimeError("failed before first window")

    _streaming(db_connection, monkeypatch, "wf", ["page"], chunk_pages, prior_chunks=["old chunk"])
    assert _texts(db_connection, "wf")
    with pytest.raises(RuntimeError, match="failed before first window"):
        await activities.create_chunks_from_db("wf")

    assert _texts(db_connection, "wf") == []
    assert db_connection.get_document("wf")["stage"] == "chunking"


async def test_second_stream_run_replaces_previous_streamed_set(db_connection, monkeypatch, minio_uploads):
    runs = iter(["first", "second"])

    async def chunk_pages(pages, config, progress_callback=None):
        text = next(runs)
        await progress_callback(_window(1, _window_chunk(text, 1)))
        return _result(config, ChunkCandidate(text, 1, 1, [1], [], 3))

    _streaming(db_connection, monkeypatch, "wf", ["page"], chunk_pages)
    await activities.create_chunks_from_db("wf")
    await activities.create_chunks_from_db("wf")

    assert _texts(db_connection, "wf") == ["second"]
    _assert_still_chunking_without_approve(db_connection, "wf")
    assert "chunk_checkpoint" not in json.loads(db_connection.get_latest_document_job("wf").get("config_json") or "{}")


async def test_deterministic_progress_emits_window_chunks():
    events = []

    async def capture(event):
        events.append(event)

    pages = [
        {"page_number": 1, "original_markdown": ("alpha " * 40).strip()},
        {"page_number": 2, "original_markdown": ("bravo " * 40).strip()},
    ]
    config = ChunkingConfig(
        provider="deterministic", model="deterministic", target_chunk_tokens=20, max_chunk_tokens=20,
        min_chunk_tokens=5, chunk_overlap_tokens=0, max_pages_per_chunk=1,
    )
    result = await DeterministicChunkingProvider().chunk_document(pages, config, progress_callback=capture)
    streamed = [item for event in events for item in (event.get("checkpoint_window_chunks") or [])]
    assert streamed, "deterministic chunking should stream emitted chunks in progress events"
    assert all("text" in item and "page_start" in item for item in streamed)
    assert result.chunks
    assert events[-1]["percent"] == 100.0
    assert events[-1]["chunks_emitted"] == len(result.chunks)


async def test_deterministic_chunking_without_callback_still_returns_chunks():
    pages = [{"page_number": 1, "original_markdown": "A short page of text for chunking."}]
    config = ChunkingConfig(
        provider="deterministic", model="deterministic", min_chunk_tokens=1, max_chunk_tokens=50,
        target_chunk_tokens=50, chunk_overlap_tokens=0,
    )
    result = await DeterministicChunkingProvider().chunk_document(pages, config)
    assert len(result.chunks) >= 1
