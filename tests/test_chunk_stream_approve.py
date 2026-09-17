"""Streaming chunk persist + delayed Approve Chunks (#157)."""

from __future__ import annotations

import json

import pytest

from pipeline.services.documents import list_available_actions


def _window_chunk(text: str, page: int, token_count: int = 3) -> dict:
    return {
        "text": text,
        "page_start": page,
        "page_end": page,
        "source_page_numbers": [page],
        "source_spans": [],
        "token_count": token_count,
        "section_title": "",
        "content_type": "body",
        "is_reference": False,
    }


def _seed_chunking_doc(db, workflow_id: str, *, pages: list[dict], prior_chunks: list[dict] | None = None):
    db.upsert_document(
        workflow_id=workflow_id,
        document_id=f"doc-{workflow_id}",
        filename="doc.pdf",
        filepath="/tmp/doc.pdf",
        stage="chunking",
        page_count=len(pages),
        chunk_count=len(prior_chunks or []),
    )
    db.save_pages(workflow_id, pages)
    if prior_chunks:
        db.save_chunks(workflow_id, prior_chunks)
    db.create_document_job(
        workflow_id=workflow_id,
        job_type="pipeline",
        status="running",
        current_stage="chunking",
        config={"source": "pipeline"},
    )


def _patch_chunking(monkeypatch, activities, tmp_path, fake_chunk_pages, *, min_pages: str = "500"):
    from pipeline.chunking.base import ChunkingConfig

    monkeypatch.setenv("CHUNKING_CHECKPOINT_MIN_PAGES", min_pages)
    cfg = ChunkingConfig(
        provider="deterministic",
        model="deterministic",
        fallback_provider="deterministic",
    )
    monkeypatch.setattr(activities, "load_chunking_config", lambda **_kwargs: cfg)
    monkeypatch.setattr(activities, "chunk_pages", fake_chunk_pages)
    monkeypatch.setattr(
        activities,
        "_upload_file_to_minio",
        lambda *args, **kwargs: ("minio://documents/fake/chunks.json", 2, "application/json"),
    )

    def fake_write_json(data):
        path = tmp_path / "chunks-stream.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    monkeypatch.setattr(activities, "_write_json_temp", fake_write_json)
    return cfg


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_window_chunks_do_not_append_rows(db_connection, monkeypatch, tmp_path):
    from pipeline.chunking.base import ChunkCandidate, ChunkingResult
    import pipeline.temporal.document_tasks as activities
    import pipeline.db as db_module

    workflow_id = "wf-stream-empty-window"
    _seed_chunking_doc(
        db_connection,
        workflow_id,
        pages=[{"page_number": 1, "original_markdown": "page one"}],
    )
    append_calls = []
    original_append = db_module.append_chunk_checkpoint

    def spy_append(workflow_id_arg, chunks):
        append_calls.append(list(chunks))
        return original_append(workflow_id_arg, chunks)

    monkeypatch.setattr(db_module, "append_chunk_checkpoint", spy_append)

    async def fake_chunk_pages(pages, config, progress_callback=None):
        await progress_callback(
            {
                "provider": config.provider,
                "pages_processed": 1,
                "pages_total": 1,
                "chunks_emitted": 0,
                "percent": 50.0,
                "window_succeeded": True,
                "checkpoint_window_chunks": [],
            }
        )
        return ChunkingResult(
            chunks=[ChunkCandidate("final", 1, 1, [1], [], 2)],
            provider=config.provider,
            model=config.model,
            config=config,
            warnings=[],
            stats={"chunk_count": 1},
        )

    _patch_chunking(monkeypatch, activities, tmp_path, fake_chunk_pages)
    result = await activities.create_chunks_from_db(workflow_id)
    assert result["chunk_count"] == 1
    assert append_calls == []
    stored = db_connection.get_chunks(workflow_id, include_excluded=True)
    assert [row["original_text"] for row in stored] == ["final"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_drops_adjacent_duplicate_text(db_connection, monkeypatch, tmp_path):
    from pipeline.chunking.base import ChunkCandidate, ChunkingResult
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-stream-dedupe"
    _seed_chunking_doc(
        db_connection,
        workflow_id,
        pages=[
            {"page_number": 1, "original_markdown": "same"},
            {"page_number": 2, "original_markdown": "same"},
        ],
    )

    streamed_count = {"value": 0}

    async def fake_chunk_pages(pages, config, progress_callback=None):
        await progress_callback(
            {
                "provider": config.provider,
                "pages_processed": 2,
                "pages_total": 2,
                "chunks_emitted": 2,
                "percent": 100.0,
                "window_succeeded": True,
                "checkpoint_window_chunks": [
                    _window_chunk("same text", 1),
                    _window_chunk("same text", 2),
                ],
            }
        )
        streamed_count["value"] = len(db_connection.get_chunks(workflow_id, include_excluded=True))
        return ChunkingResult(
            chunks=[ChunkCandidate("same text", 1, 2, [1, 2], [], 3)],
            provider=config.provider,
            model=config.model,
            config=config,
            warnings=[],
            stats={"chunk_count": 1},
        )

    _patch_chunking(monkeypatch, activities, tmp_path, fake_chunk_pages)
    result = await activities.create_chunks_from_db(workflow_id)
    assert streamed_count["value"] == 1
    assert result["chunk_count"] == 1
    stored = db_connection.get_chunks(workflow_id, include_excluded=True)
    assert [row["original_text"] for row in stored] == ["same text"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_persists_sparse_candidate_fields(db_connection, monkeypatch, tmp_path):
    from pipeline.chunking.base import ChunkCandidate, ChunkingResult
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-stream-sparse"
    _seed_chunking_doc(
        db_connection,
        workflow_id,
        pages=[{"page_number": 1, "original_markdown": "page"}],
    )
    mid_count = {"value": 0, "token_count": None}

    async def fake_chunk_pages(pages, config, progress_callback=None):
        await progress_callback(
            {
                "provider": config.provider,
                "pages_processed": 1,
                "pages_total": 1,
                "chunks_emitted": 1,
                "percent": 50.0,
                "checkpoint_window_chunks": [{"text": "hello"}],
            }
        )
        streamed = db_connection.get_chunks(workflow_id, include_excluded=True)
        mid_count["value"] = len(streamed)
        mid_count["token_count"] = int(streamed[0]["token_count"] or 0)
        return ChunkingResult(
            chunks=[ChunkCandidate("hello", 1, 1, [1], [], 1)],
            provider=config.provider,
            model=config.model,
            config=config,
            warnings=[],
            stats={"chunk_count": 1},
        )

    _patch_chunking(monkeypatch, activities, tmp_path, fake_chunk_pages)
    result = await activities.create_chunks_from_db(workflow_id)
    assert mid_count["value"] == 1
    assert mid_count["token_count"] == 0
    assert result["chunk_count"] == 1
    row = db_connection.get_chunks(workflow_id, include_excluded=True)[0]
    assert row["original_text"] == "hello"
    assert int(row["page_start"] or 0) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stream_crash_keeps_partial_rows_and_blocks_approve(db_connection, monkeypatch, tmp_path):
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-stream-crash"
    _seed_chunking_doc(
        db_connection,
        workflow_id,
        pages=[
            {"page_number": 1, "original_markdown": "one"},
            {"page_number": 2, "original_markdown": "two"},
        ],
    )

    async def fake_chunk_pages(pages, config, progress_callback=None):
        await progress_callback(
            {
                "provider": config.provider,
                "pages_processed": 1,
                "pages_total": 2,
                "chunks_emitted": 1,
                "percent": 50.0,
                "checkpoint_window_chunks": [_window_chunk("streamed one", 1)],
            }
        )
        raise RuntimeError("chunker crashed")

    _patch_chunking(monkeypatch, activities, tmp_path, fake_chunk_pages)
    with pytest.raises(RuntimeError, match="chunker crashed"):
        await activities.create_chunks_from_db(workflow_id)

    stored = db_connection.get_chunks(workflow_id, include_excluded=True)
    assert [row["original_text"] for row in stored] == ["streamed one"]
    doc = db_connection.get_document(workflow_id)
    job = db_connection.get_latest_document_job(workflow_id)
    assert doc["stage"] == "chunking"
    assert "approve_chunks" not in list_available_actions(doc, job)
    db_connection.reconcile_materialized_state(workflow_id)
    refreshed = db_connection.get_document(workflow_id)
    assert refreshed["stage"] == "chunking"
    assert "approve_chunks" not in list_available_actions(refreshed, db_connection.get_latest_document_job(workflow_id))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_checkpoint_start_clears_prior_chunks_before_provider_runs(db_connection, monkeypatch, tmp_path):
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-stream-reset"
    _seed_chunking_doc(
        db_connection,
        workflow_id,
        pages=[{"page_number": 1, "original_markdown": "page"}],
        prior_chunks=[
            {
                "chunk_number": 1,
                "original_text": "old chunk",
                "token_count": 2,
                "page_start": 1,
                "page_end": 1,
            }
        ],
    )
    assert db_connection.get_chunks(workflow_id, include_excluded=True)

    async def fake_chunk_pages(pages, config, progress_callback=None):
        raise RuntimeError("failed before first window")

    _patch_chunking(monkeypatch, activities, tmp_path, fake_chunk_pages)
    with pytest.raises(RuntimeError, match="failed before first window"):
        await activities.create_chunks_from_db(workflow_id)

    assert db_connection.get_chunks(workflow_id, include_excluded=True) == []
    doc = db_connection.get_document(workflow_id)
    assert doc["stage"] == "chunking"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_second_stream_run_replaces_previous_streamed_set(db_connection, monkeypatch, tmp_path):
    from pipeline.chunking.base import ChunkCandidate, ChunkingResult
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-stream-rerun"
    _seed_chunking_doc(
        db_connection,
        workflow_id,
        pages=[{"page_number": 1, "original_markdown": "page"}],
    )
    call = {"n": 0}

    async def fake_chunk_pages(pages, config, progress_callback=None):
        call["n"] += 1
        text = "first" if call["n"] == 1 else "second"
        await progress_callback(
            {
                "provider": config.provider,
                "pages_processed": 1,
                "pages_total": 1,
                "chunks_emitted": 1,
                "percent": 100.0,
                "checkpoint_window_chunks": [_window_chunk(text, 1)],
            }
        )
        return ChunkingResult(
            chunks=[ChunkCandidate(text, 1, 1, [1], [], 3)],
            provider=config.provider,
            model=config.model,
            config=config,
            warnings=[],
            stats={"chunk_count": 1},
        )

    _patch_chunking(monkeypatch, activities, tmp_path, fake_chunk_pages)
    await activities.create_chunks_from_db(workflow_id)
    await activities.create_chunks_from_db(workflow_id)
    stored = db_connection.get_chunks(workflow_id, include_excluded=True)
    assert [row["original_text"] for row in stored] == ["second"]
    doc = db_connection.get_document(workflow_id)
    job = db_connection.get_latest_document_job(workflow_id)
    assert doc["stage"] == "chunking"
    assert "approve_chunks" not in list_available_actions(doc, job)
    cfg = json.loads(job.get("config_json") or "{}")
    assert "chunk_checkpoint" not in cfg


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deterministic_progress_emits_window_chunks():
    from pipeline.chunking.base import ChunkingConfig
    from pipeline.chunking.deterministic import DeterministicChunkingProvider

    events = []

    async def capture(event):
        events.append(event)

    pages = [
        {"page_number": 1, "original_markdown": ("alpha " * 40).strip()},
        {"page_number": 2, "original_markdown": ("bravo " * 40).strip()},
    ]
    config = ChunkingConfig(
        provider="deterministic",
        model="deterministic",
        target_chunk_tokens=20,
        max_chunk_tokens=20,
        min_chunk_tokens=5,
        chunk_overlap_tokens=0,
        max_pages_per_chunk=1,
    )
    result = await DeterministicChunkingProvider().chunk_document(
        pages, config, progress_callback=capture
    )
    streamed = [item for event in events for item in (event.get("checkpoint_window_chunks") or [])]
    assert streamed, "deterministic chunking should stream emitted chunks in progress events"
    assert all("text" in item and "page_start" in item for item in streamed)
    assert result.chunks
    assert events[-1]["percent"] == 100.0
    assert events[-1]["chunks_emitted"] == len(result.chunks)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deterministic_chunking_without_callback_still_returns_chunks():
    from pipeline.chunking.base import ChunkingConfig
    from pipeline.chunking.deterministic import DeterministicChunkingProvider

    pages = [{"page_number": 1, "original_markdown": "A short page of text for chunking."}]
    config = ChunkingConfig(
        provider="deterministic",
        model="deterministic",
        min_chunk_tokens=1,
        max_chunk_tokens=50,
        target_chunk_tokens=50,
        chunk_overlap_tokens=0,
    )
    result = await DeterministicChunkingProvider().chunk_document(pages, config)
    assert len(result.chunks) >= 1
