"""Regression tests for issue #125 chunk checkpoint/resume behavior."""

from __future__ import annotations

import json

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chunking_checkpoint_persists_and_resumes(db_connection, monkeypatch, tmp_path):
    from pipeline.chunking.base import ChunkCandidate, ChunkingConfig, ChunkingResult
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-chkpt-resume"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-chkpt-resume",
        filename="doc.pdf",
        filepath="/tmp/doc.pdf",
        stage="chunking",
    )
    db_connection.save_pages(
        workflow_id,
        [
            {"page_number": 1, "original_markdown": "page one content"},
            {"page_number": 2, "original_markdown": "page two content"},
        ],
    )
    db_connection.create_document_job(
        workflow_id=workflow_id,
        job_type="chunk_retry",
        status="running",
        current_stage="chunking_processing",
        config={"source": "api_retry_chunking"},
    )

    monkeypatch.setenv("CHUNKING_CHECKPOINT_MIN_PAGES", "1")
    monkeypatch.setenv("CHUNKING_CHECKPOINT_WINDOWS", "1")

    cfg = ChunkingConfig(
        provider="openai_vllm",
        model="gemma-4",
        endpoint="http://chunker.test/v1",
        page_window_size=1,
        fallback_provider="openai_vllm",
    )
    monkeypatch.setattr(activities, "load_chunking_config", lambda **_kwargs: cfg)

    call_state = {"count": 0, "pages_seen": []}

    async def fake_chunk_pages(pages, config, progress_callback=None):
        call_state["count"] += 1
        call_state["pages_seen"].append([p.get("page_number") for p in pages])
        if call_state["count"] == 1:
            await progress_callback(
                {
                    "provider": config.provider,
                    "windows_processed": 1,
                    "windows_total": 2,
                    "pages_processed": 1,
                    "pages_total": 2,
                    "chunks_emitted": 1,
                    "percent": 50.0,
                    "window_succeeded": True,
                    "checkpoint_window_chunks": [
                        {
                            "text": "chunk one",
                            "page_start": 1,
                            "page_end": 1,
                            "source_page_numbers": [1],
                            "source_spans": [],
                            "token_count": 2,
                            "section_title": "",
                            "content_type": "body",
                            "is_reference": False,
                        }
                    ],
                }
            )
            raise RuntimeError("forced checkpoint crash")
        await progress_callback(
            {
                "provider": config.provider,
                "windows_processed": 1,
                "windows_total": 1,
                "pages_processed": 1,
                "pages_total": 1,
                "chunks_emitted": 1,
                "percent": 100.0,
                "window_succeeded": True,
                "checkpoint_window_chunks": [
                    {
                        "text": "chunk two",
                        "page_start": 2,
                        "page_end": 2,
                        "source_page_numbers": [2],
                        "source_spans": [],
                        "token_count": 2,
                        "section_title": "",
                        "content_type": "body",
                        "is_reference": False,
                    }
                ],
            }
        )
        return ChunkingResult(
            chunks=[
                ChunkCandidate(
                    text="chunk two",
                    page_start=2,
                    page_end=2,
                    source_page_numbers=[2],
                    source_spans=[],
                    token_count=2,
                )
            ],
            provider=config.provider,
            model=config.model,
            config=config,
            warnings=[],
            stats={"chunk_count": 1},
        )

    monkeypatch.setattr(activities, "chunk_pages", fake_chunk_pages)
    monkeypatch.setattr(
        activities,
        "_upload_file_to_minio",
        lambda *args, **kwargs: ("minio://documents/fake/chunks.json", 2, "application/json"),
    )

    def fake_write_json(data):
        p = tmp_path / "chunks.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    monkeypatch.setattr(activities, "_write_json_temp", fake_write_json)

    with pytest.raises(RuntimeError, match="forced checkpoint crash"):
        await activities.create_chunks_from_db(workflow_id)

    chunks_after_failure = db_connection.get_chunks(workflow_id, include_excluded=True)
    assert len(chunks_after_failure) == 1
    assert chunks_after_failure[0]["original_text"] == "chunk one"

    result = await activities.create_chunks_from_db(workflow_id)
    assert result["chunk_count"] == 2
    assert call_state["pages_seen"][0] == [1, 2]
    assert call_state["pages_seen"][1] == [2]

    final_chunks = db_connection.get_chunks(workflow_id, include_excluded=True)
    assert [c["chunk_number"] for c in final_chunks] == [1, 2]
    assert [c["original_text"] for c in final_chunks] == ["chunk one", "chunk two"]

    latest_job = db_connection.get_latest_document_job(workflow_id)
    cfg_json = json.loads(latest_job.get("config_json") or "{}")
    checkpoint = cfg_json.get("chunk_checkpoint") or {}
    assert checkpoint.get("status") == "completed"
    assert int(checkpoint.get("windows_completed") or 0) == 2
    assert int(checkpoint.get("chunks_persisted") or 0) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chunking_small_docs_keep_single_save_path(db_connection, monkeypatch, tmp_path):
    from pipeline.chunking.base import ChunkCandidate, ChunkingConfig, ChunkingResult
    import pipeline.temporal.document_tasks as activities
    import pipeline.db as db_module

    workflow_id = "wf-chkpt-small-doc"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-chkpt-small-doc",
        filename="doc.pdf",
        filepath="/tmp/doc.pdf",
        stage="chunking",
    )
    db_connection.save_pages(
        workflow_id,
        [{"page_number": 1, "original_markdown": "single page"}],
    )
    db_connection.create_document_job(
        workflow_id=workflow_id,
        job_type="chunk_retry",
        status="running",
        current_stage="chunking_processing",
        config={"source": "api_retry_chunking"},
    )

    monkeypatch.setenv("CHUNKING_CHECKPOINT_MIN_PAGES", "500")
    monkeypatch.setenv("CHUNKING_CHECKPOINT_WINDOWS", "1")

    cfg = ChunkingConfig(
        provider="openai_vllm",
        model="gemma-4",
        endpoint="http://chunker.test/v1",
        page_window_size=1,
        fallback_provider="openai_vllm",
    )
    monkeypatch.setattr(activities, "load_chunking_config", lambda **_kwargs: cfg)
    async def fake_chunk_pages_small(pages, config, progress_callback=None):
        return ChunkingResult(
            chunks=[
                ChunkCandidate(
                    text="single chunk",
                    page_start=1,
                    page_end=1,
                    source_page_numbers=[1],
                    source_spans=[],
                    token_count=2,
                )
            ],
            provider=config.provider,
            model=config.model,
            config=config,
            warnings=[],
            stats={"chunk_count": 1},
        )

    monkeypatch.setattr(activities, "chunk_pages", fake_chunk_pages_small)
    append_called = {"value": False}

    def fail_if_append(*_args, **_kwargs):
        append_called["value"] = True
        raise AssertionError("append_chunk_checkpoint should not be used for small docs")

    monkeypatch.setattr(db_module, "append_chunk_checkpoint", fail_if_append)
    monkeypatch.setattr(
        activities,
        "_upload_file_to_minio",
        lambda *args, **kwargs: ("minio://documents/fake/chunks.json", 2, "application/json"),
    )

    def fake_write_json(data):
        p = tmp_path / "chunks-small.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    monkeypatch.setattr(activities, "_write_json_temp", fake_write_json)

    result = await activities.create_chunks_from_db(workflow_id)
    assert result["chunk_count"] == 1
    assert append_called["value"] is False
    chunks = db_connection.get_chunks(workflow_id, include_excluded=True)
    assert len(chunks) == 1
    latest_job = db_connection.get_latest_document_job(workflow_id)
    cfg_json = json.loads(latest_job.get("config_json") or "{}")
    assert "chunk_checkpoint" not in cfg_json


@pytest.mark.unit
@pytest.mark.asyncio
async def test_checkpoint_windows_flush_interval_and_final_flush(db_connection, monkeypatch, tmp_path):
    from pipeline.chunking.base import ChunkCandidate, ChunkingConfig, ChunkingResult
    import pipeline.temporal.document_tasks as activities
    import pipeline.db as db_module

    workflow_id = "wf-chkpt-flush-interval"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-chkpt-flush-interval",
        filename="doc.pdf",
        filepath="/tmp/doc.pdf",
        stage="chunking",
    )
    db_connection.save_pages(
        workflow_id,
        [
            {"page_number": 1, "original_markdown": "page one content"},
            {"page_number": 2, "original_markdown": "page two content"},
            {"page_number": 3, "original_markdown": "page three content"},
        ],
    )
    db_connection.create_document_job(
        workflow_id=workflow_id,
        job_type="chunk_retry",
        status="running",
        current_stage="chunking_processing",
        config={"source": "api_retry_chunking"},
    )

    monkeypatch.setenv("CHUNKING_CHECKPOINT_MIN_PAGES", "1")
    monkeypatch.setenv("CHUNKING_CHECKPOINT_WINDOWS", "2")

    cfg = ChunkingConfig(
        provider="openai_vllm",
        model="gemma-4",
        endpoint="http://chunker.test/v1",
        page_window_size=1,
        fallback_provider="openai_vllm",
    )
    monkeypatch.setattr(activities, "load_chunking_config", lambda **_kwargs: cfg)

    async def fake_chunk_pages(pages, config, progress_callback=None):
        await progress_callback(
            {
                "provider": config.provider,
                "windows_processed": 1,
                "windows_total": 3,
                "pages_processed": 1,
                "pages_total": 3,
                "chunks_emitted": 1,
                "percent": 33.0,
                "window_succeeded": True,
                "checkpoint_window_chunks": [
                    {
                        "text": "chunk one",
                        "page_start": 1,
                        "page_end": 1,
                        "source_page_numbers": [1],
                        "source_spans": [],
                        "token_count": 2,
                        "section_title": "",
                        "content_type": "body",
                        "is_reference": False,
                    }
                ],
            }
        )
        await progress_callback(
            {
                "provider": config.provider,
                "windows_processed": 2,
                "windows_total": 3,
                "pages_processed": 2,
                "pages_total": 3,
                "chunks_emitted": 2,
                "percent": 66.0,
                "window_succeeded": True,
                "checkpoint_window_chunks": [
                    {
                        "text": "chunk two",
                        "page_start": 2,
                        "page_end": 2,
                        "source_page_numbers": [2],
                        "source_spans": [],
                        "token_count": 2,
                        "section_title": "",
                        "content_type": "body",
                        "is_reference": False,
                    }
                ],
            }
        )
        await progress_callback(
            {
                "provider": config.provider,
                "windows_processed": 3,
                "windows_total": 3,
                "pages_processed": 3,
                "pages_total": 3,
                "chunks_emitted": 3,
                "percent": 100.0,
                "window_succeeded": True,
                "checkpoint_window_chunks": [
                    {
                        "text": "chunk three",
                        "page_start": 3,
                        "page_end": 3,
                        "source_page_numbers": [3],
                        "source_spans": [],
                        "token_count": 2,
                        "section_title": "",
                        "content_type": "body",
                        "is_reference": False,
                    }
                ],
            }
        )
        return ChunkingResult(
            chunks=[
                ChunkCandidate("chunk one", 1, 1, [1], [], 2),
                ChunkCandidate("chunk two", 2, 2, [2], [], 2),
                ChunkCandidate("chunk three", 3, 3, [3], [], 2),
            ],
            provider=config.provider,
            model=config.model,
            config=config,
            warnings=[],
            stats={"chunk_count": 3},
        )

    monkeypatch.setattr(activities, "chunk_pages", fake_chunk_pages)
    flush_sizes: list[int] = []
    original_append = db_module.append_chunk_checkpoint

    def spy_append(workflow_id_arg, chunks):
        flush_sizes.append(len(chunks))
        return original_append(workflow_id_arg, chunks)

    monkeypatch.setattr(db_module, "append_chunk_checkpoint", spy_append)
    monkeypatch.setattr(
        activities,
        "_upload_file_to_minio",
        lambda *args, **kwargs: ("minio://documents/fake/chunks.json", 2, "application/json"),
    )

    def fake_write_json(data):
        p = tmp_path / "chunks-flush.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    monkeypatch.setattr(activities, "_write_json_temp", fake_write_json)

    result = await activities.create_chunks_from_db(workflow_id)
    assert result["chunk_count"] == 3
    assert flush_sizes == [2, 1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_checkpoint_resume_uses_next_page_offset_for_empty_windows(db_connection, monkeypatch, tmp_path):
    from pipeline.chunking.base import ChunkCandidate, ChunkingConfig, ChunkingResult
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-chkpt-empty-window-resume"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-chkpt-empty-window-resume",
        filename="doc.pdf",
        filepath="/tmp/doc.pdf",
        stage="chunking",
    )
    db_connection.save_pages(
        workflow_id,
        [
            {"page_number": 1, "original_markdown": ""},
            {"page_number": 2, "original_markdown": "page two content"},
        ],
    )
    db_connection.create_document_job(
        workflow_id=workflow_id,
        job_type="chunk_retry",
        status="running",
        current_stage="chunking_processing",
        config={"source": "api_retry_chunking"},
    )

    monkeypatch.setenv("CHUNKING_CHECKPOINT_MIN_PAGES", "1")
    monkeypatch.setenv("CHUNKING_CHECKPOINT_WINDOWS", "1")

    cfg = ChunkingConfig(
        provider="openai_vllm",
        model="gemma-4",
        endpoint="http://chunker.test/v1",
        page_window_size=1,
        fallback_provider="openai_vllm",
    )
    monkeypatch.setattr(activities, "load_chunking_config", lambda **_kwargs: cfg)

    calls = {"count": 0, "pages_seen": []}

    async def fake_chunk_pages(pages, config, progress_callback=None):
        calls["count"] += 1
        calls["pages_seen"].append([p.get("page_number") for p in pages])
        if calls["count"] == 1:
            await progress_callback(
                {
                    "provider": config.provider,
                    "windows_processed": 1,
                    "windows_total": 2,
                    "pages_processed": 1,
                    "pages_total": 2,
                    "chunks_emitted": 0,
                    "percent": 50.0,
                    "window_succeeded": True,
                    "checkpoint_window_chunks": [],
                }
            )
            raise RuntimeError("forced empty-window crash")

        await progress_callback(
            {
                "provider": config.provider,
                "windows_processed": 1,
                "windows_total": 1,
                "pages_processed": 1,
                "pages_total": 1,
                "chunks_emitted": 1,
                "percent": 100.0,
                "window_succeeded": True,
                "checkpoint_window_chunks": [
                    {
                        "text": "chunk two",
                        "page_start": 2,
                        "page_end": 2,
                        "source_page_numbers": [2],
                        "source_spans": [],
                        "token_count": 2,
                        "section_title": "",
                        "content_type": "body",
                        "is_reference": False,
                    }
                ],
            }
        )
        return ChunkingResult(
            chunks=[ChunkCandidate("chunk two", 2, 2, [2], [], 2)],
            provider=config.provider,
            model=config.model,
            config=config,
            warnings=[],
            stats={"chunk_count": 1},
        )

    monkeypatch.setattr(activities, "chunk_pages", fake_chunk_pages)
    monkeypatch.setattr(
        activities,
        "_upload_file_to_minio",
        lambda *args, **kwargs: ("minio://documents/fake/chunks.json", 2, "application/json"),
    )

    def fake_write_json(data):
        p = tmp_path / "chunks-empty-resume.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    monkeypatch.setattr(activities, "_write_json_temp", fake_write_json)

    with pytest.raises(RuntimeError, match="empty-window crash"):
        await activities.create_chunks_from_db(workflow_id)
    result = await activities.create_chunks_from_db(workflow_id)
    assert result["chunk_count"] == 1
    assert calls["pages_seen"] == [[1, 2], [2]]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_completed_checkpoint_does_not_rerun_llm_windows(db_connection, monkeypatch, tmp_path):
    from pipeline.chunking.base import ChunkCandidate, ChunkingConfig, ChunkingResult
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-chkpt-completed-no-rerun"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-chkpt-completed-no-rerun",
        filename="doc.pdf",
        filepath="/tmp/doc.pdf",
        stage="chunking",
    )
    db_connection.save_pages(
        workflow_id,
        [
            {"page_number": 1, "original_markdown": "page one content"},
            {"page_number": 2, "original_markdown": "page two content"},
        ],
    )
    db_connection.create_document_job(
        workflow_id=workflow_id,
        job_type="chunk_retry",
        status="running",
        current_stage="chunking_processing",
        config={"source": "api_retry_chunking"},
    )

    monkeypatch.setenv("CHUNKING_CHECKPOINT_MIN_PAGES", "1")
    monkeypatch.setenv("CHUNKING_CHECKPOINT_WINDOWS", "1")

    cfg = ChunkingConfig(
        provider="openai_vllm",
        model="gemma-4",
        endpoint="http://chunker.test/v1",
        page_window_size=1,
        fallback_provider="openai_vllm",
    )
    monkeypatch.setattr(activities, "load_chunking_config", lambda **_kwargs: cfg)

    calls = {"pages_seen": []}

    async def fake_chunk_pages(pages, config, progress_callback=None):
        calls["pages_seen"].append([p.get("page_number") for p in pages])
        if pages:
            await progress_callback(
                {
                    "provider": config.provider,
                    "windows_processed": 1,
                    "windows_total": 2,
                    "pages_processed": 1,
                    "pages_total": 2,
                    "chunks_emitted": 1,
                    "percent": 50.0,
                    "window_succeeded": True,
                    "checkpoint_window_chunks": [
                        {
                            "text": "chunk one",
                            "page_start": 1,
                            "page_end": 1,
                            "source_page_numbers": [1],
                            "source_spans": [],
                            "token_count": 2,
                            "section_title": "",
                            "content_type": "body",
                            "is_reference": False,
                        }
                    ],
                }
            )
            await progress_callback(
                {
                    "provider": config.provider,
                    "windows_processed": 2,
                    "windows_total": 2,
                    "pages_processed": 2,
                    "pages_total": 2,
                    "chunks_emitted": 2,
                    "percent": 100.0,
                    "window_succeeded": True,
                    "checkpoint_window_chunks": [
                        {
                            "text": "chunk two",
                            "page_start": 2,
                            "page_end": 2,
                            "source_page_numbers": [2],
                            "source_spans": [],
                            "token_count": 2,
                            "section_title": "",
                            "content_type": "body",
                            "is_reference": False,
                        }
                    ],
                }
            )
            return ChunkingResult(
                chunks=[
                    ChunkCandidate("chunk one", 1, 1, [1], [], 2),
                    ChunkCandidate("chunk two", 2, 2, [2], [], 2),
                ],
                provider=config.provider,
                model=config.model,
                config=config,
                warnings=[],
                stats={"chunk_count": 2},
            )
        return ChunkingResult(
            chunks=[],
            provider=config.provider,
            model=config.model,
            config=config,
            warnings=[],
            stats={"chunk_count": 0},
        )

    monkeypatch.setattr(activities, "chunk_pages", fake_chunk_pages)
    upload_calls = {"count": 0}

    def flaky_upload(*args, **kwargs):
        upload_calls["count"] += 1
        if upload_calls["count"] == 1:
            raise RuntimeError("forced upload failure")
        return ("minio://documents/fake/chunks.json", 2, "application/json")

    monkeypatch.setattr(activities, "_upload_file_to_minio", flaky_upload)

    def fake_write_json(data):
        p = tmp_path / "chunks-completed-resume.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    monkeypatch.setattr(activities, "_write_json_temp", fake_write_json)

    with pytest.raises(RuntimeError, match="forced upload failure"):
        await activities.create_chunks_from_db(workflow_id)

    result = await activities.create_chunks_from_db(workflow_id)
    assert result["chunk_count"] == 2
    assert calls["pages_seen"][0] == [1, 2]
    assert calls["pages_seen"][1] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_chunking_creates_job_before_workflow_start(monkeypatch):
    from pipeline.routers import documents_actions as action_routes

    monkeypatch.setattr(
        action_routes.access,
        "require_document_for_user",
        lambda workflow_id, user, permission: {
            "workflow_id": workflow_id,
            "document_id": "doc-1",
            "filename": "doc.pdf",
            "instance": "tenant-a",
            "page_count": 12,
        },
    )
    monkeypatch.setattr(action_routes.db, "get_pages", lambda _workflow_id: [{"page_number": 1}])
    call_order: list[str] = []

    def _create_document_job(**kwargs):
        call_order.append("create_job")
        return 321

    async def _start_chunking_retry(**kwargs):
        call_order.append("start_workflow")
        assert kwargs["args"][-1] == 321

    monkeypatch.setattr(action_routes.db, "create_document_job", _create_document_job)
    monkeypatch.setattr(action_routes.workflow_runtime, "start_chunking_retry", _start_chunking_retry)
    monkeypatch.setattr(action_routes.db, "update_document_fields", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(action_routes.db, "update_document_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(action_routes.db, "log_audit", lambda **kwargs: None)

    result = await action_routes.retry_chunking("wf-1", object())
    assert result["status"] == "started"
    assert call_order == ["create_job", "start_workflow"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_checkpoint_finalize_applies_chunk_limit_guards(db_connection, monkeypatch, tmp_path):
    """Checkpoint rows are raw provider candidates, so finalize must enforce #115."""
    from pipeline.chunking.base import ChunkCandidate, ChunkingConfig, ChunkingResult
    from pipeline.chunking.enforce_limits import (
        MARQO_MAX_DOCUMENT_BYTES,
        enforce_chunk_limits,
        estimate_marqo_tensor_payload_bytes,
    )
    import pipeline.temporal.document_tasks as activities

    workflow_id = "wf-chkpt-enforce"
    db_connection.upsert_document(
        workflow_id=workflow_id,
        document_id="doc-chkpt-enforce",
        filename="big.pdf",
        filepath="/tmp/big.pdf",
        stage="chunking",
    )
    db_connection.save_pages(workflow_id, [{"page_number": 1, "original_markdown": "page one"}])
    db_connection.create_document_job(
        workflow_id=workflow_id,
        job_type="chunk_retry",
        status="running",
        current_stage="chunking_processing",
        config={"source": "api_retry_chunking"},
    )

    monkeypatch.setenv("CHUNKING_CHECKPOINT_MIN_PAGES", "1")
    monkeypatch.setenv("CHUNKING_CHECKPOINT_WINDOWS", "1")

    cfg = ChunkingConfig(
        provider="openai_vllm",
        model="gemma-4",
        endpoint="http://chunker.test/v1",
        page_window_size=1,
        fallback_provider="openai_vllm",
        max_chunk_tokens=450,
    )
    monkeypatch.setattr(activities, "load_chunking_config", lambda **_kwargs: cfg)

    # One window the grouper merged into a single chunk far above both guards.
    oversized_text = "sentence about cattle feed. " * 6000
    assert estimate_marqo_tensor_payload_bytes(oversized_text) > MARQO_MAX_DOCUMENT_BYTES

    async def fake_chunk_pages(pages, config, progress_callback=None):
        # Mirrors chunking.service.chunk_pages: the provider emits raw candidates
        # through the callback, and enforcement only touches the return value.
        if progress_callback:
            await progress_callback(
                {
                    "provider": config.provider,
                    "windows_processed": 1,
                    "windows_total": 1,
                    "pages_processed": 1,
                    "pages_total": 1,
                    "chunks_emitted": 1,
                    "percent": 100.0,
                    "window_succeeded": True,
                    "checkpoint_window_chunks": [
                        {
                            "text": oversized_text,
                            "page_start": 1,
                            "page_end": 1,
                            "source_page_numbers": [1],
                            "source_spans": [],
                            "token_count": 40000,
                            "section_title": "",
                            "content_type": "body",
                            "is_reference": False,
                        }
                    ],
                }
            )
        return enforce_chunk_limits(
            ChunkingResult(
                chunks=[
                    ChunkCandidate(
                        text=oversized_text,
                        page_start=1,
                        page_end=1,
                        source_page_numbers=[1],
                        source_spans=[],
                        token_count=40000,
                    )
                ],
                provider=config.provider,
                model=config.model,
                config=config,
            )
        )

    monkeypatch.setattr(activities, "chunk_pages", fake_chunk_pages)
    monkeypatch.setattr(
        activities,
        "_upload_file_to_minio",
        lambda *args, **kwargs: ("minio://documents/fake/chunks.json", 2, "application/json"),
    )

    def fake_write_json(data):
        path = tmp_path / "chunks.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    monkeypatch.setattr(activities, "_write_json_temp", fake_write_json)

    result = await activities.create_chunks_from_db(workflow_id)

    stored = db_connection.get_chunks(workflow_id, include_excluded=True)
    assert len(stored) > 1, "oversized checkpoint chunk should have been split at finalize"
    assert all(int(c["token_count"] or 0) <= cfg.max_chunk_tokens for c in stored)
    assert all(
        estimate_marqo_tensor_payload_bytes(c["original_text"]) <= MARQO_MAX_DOCUMENT_BYTES
        for c in stored
    )
    assert [c["chunk_number"] for c in stored] == list(range(1, len(stored) + 1))
    assert result["chunk_count"] == len(stored)
    assert all(json.loads(c["source_page_numbers_json"]) == [1] for c in stored)
