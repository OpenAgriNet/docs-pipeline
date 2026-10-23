"""Checkpointed chunking (#125): a crashed run resumes from its persisted windows
without re-calling the LLM for them, losing chunks or duplicating them, and the
finalized set still obeys the chunk limits (#115). Also: the routes create the job
row before they start the workflow that is bound to it."""

from __future__ import annotations

import json

import pytest

import pipeline.db as db_module
import pipeline.temporal.document_tasks as activities
from pipeline.chunking.base import ChunkCandidate, ChunkingConfig, ChunkingResult
from pipeline.storage import minio as minio_storage
from tests.helpers import seed_document

pytestmark = pytest.mark.asyncio


def _window_chunk(text: str, page: int, token_count: int = 2) -> dict:
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


def _checkpointed(db, monkeypatch, workflow_id, pages, chunk_pages, *, min_pages="1", windows="1", **config):
    """Seed a chunk-retry run over ``pages`` and route chunking to ``chunk_pages``, an
    LLM provider with one page per window (checkpointed once ``pages >= min_pages``)."""
    seed_document(db, workflow_id, stage="chunking", pages=pages)
    db.create_document_job(
        workflow_id=workflow_id, job_type="chunk_retry", status="running",
        current_stage="chunking_processing", config={"source": "api_retry_chunking"},
    )
    monkeypatch.setenv("CHUNKING_CHECKPOINT_MIN_PAGES", min_pages)
    monkeypatch.setenv("CHUNKING_CHECKPOINT_WINDOWS", windows)
    cfg = ChunkingConfig(
        provider="openai_vllm", model="gemma-4", endpoint="http://chunker.test/v1",
        page_window_size=1, fallback_provider="openai_vllm", **config,
    )
    monkeypatch.setattr(activities, "load_chunking_config", lambda **_kwargs: cfg)
    monkeypatch.setattr(activities, "chunk_pages", chunk_pages)
    return cfg


def _stored(db, workflow_id):
    return db.get_chunks(workflow_id, include_excluded=True)


def _job_config(db, workflow_id) -> dict:
    return json.loads(db.get_latest_document_job(workflow_id).get("config_json") or "{}")


@pytest.mark.parametrize("first_page, first_window, expected", [
    ("page one content", [_window_chunk("chunk one", 1)], ["chunk one", "chunk two"]),
    ("", [], ["chunk two"]),
], ids=["window-with-chunks", "empty-window-still-advances-offset"])
async def test_crashed_checkpoint_resumes_after_the_last_persisted_window(
    db_connection, monkeypatch, minio_uploads, first_page, first_window, expected
):
    seen = []

    async def chunk_pages(pages, config, progress_callback=None):
        seen.append([p["page_number"] for p in pages])
        if len(seen) == 1:
            await progress_callback(_window(1, *first_window))
            raise RuntimeError("forced checkpoint crash")
        await progress_callback(_window(1, _window_chunk("chunk two", 2)))
        return _result(config, ChunkCandidate("chunk two", 2, 2, [2], [], 2))

    _checkpointed(db_connection, monkeypatch, "wf", [first_page, "page two content"], chunk_pages)
    with pytest.raises(RuntimeError, match="forced checkpoint crash"):
        await activities.create_chunks_from_db("wf")
    assert [c["original_text"] for c in _stored(db_connection, "wf")] == [c["text"] for c in first_window]

    result = await activities.create_chunks_from_db("wf")
    assert result["chunk_count"] == len(expected)
    assert seen == [[1, 2], [2]], "the resumed run must only send the pages after the checkpoint"
    final = _stored(db_connection, "wf")
    assert [c["chunk_number"] for c in final] == list(range(1, len(expected) + 1))
    assert [c["original_text"] for c in final] == expected
    checkpoint = _job_config(db_connection, "wf")["chunk_checkpoint"]
    assert checkpoint["status"] == "completed"
    assert int(checkpoint["windows_completed"]) == 2
    assert int(checkpoint["chunks_persisted"]) == len(expected)


async def test_chunking_small_docs_keep_single_save_path(db_connection, monkeypatch, minio_uploads):
    async def chunk_pages(pages, config, progress_callback=None):
        return _result(config, ChunkCandidate("single chunk", 1, 1, [1], [], 2))

    _checkpointed(db_connection, monkeypatch, "wf", ["single page"], chunk_pages, min_pages="500")
    assert (await activities.create_chunks_from_db("wf"))["chunk_count"] == 1
    assert len(_stored(db_connection, "wf")) == 1
    assert "chunk_checkpoint" not in _job_config(db_connection, "wf")


async def test_checkpoint_windows_flush_interval_and_final_flush(db_connection, monkeypatch, minio_uploads):
    texts = ["chunk one", "chunk two", "chunk three"]

    async def chunk_pages(pages, config, progress_callback=None):
        for page, text in enumerate(texts, 1):
            await progress_callback(_window(page, _window_chunk(text, page)))
        return _result(config, *(ChunkCandidate(text, n, n, [n], [], 2) for n, text in enumerate(texts, 1)))

    pages = ["page one content", "page two content", "page three content"]
    _checkpointed(db_connection, monkeypatch, "wf", pages, chunk_pages, windows="2")
    flush_sizes: list[int] = []
    real_append = db_module.append_chunk_checkpoint

    def spy_append(workflow_id, chunks):
        flush_sizes.append(len(chunks))
        return real_append(workflow_id, chunks)

    monkeypatch.setattr(db_module, "append_chunk_checkpoint", spy_append)
    assert (await activities.create_chunks_from_db("wf"))["chunk_count"] == 3
    assert flush_sizes == [2, 1]


async def test_completed_checkpoint_does_not_rerun_llm_windows(db_connection, monkeypatch):
    seen = []

    async def chunk_pages(pages, config, progress_callback=None):
        seen.append([p["page_number"] for p in pages])
        if not pages:
            return _result(config)
        await progress_callback(_window(1, _window_chunk("chunk one", 1)))
        await progress_callback(_window(2, _window_chunk("chunk two", 2)))
        return _result(
            config, ChunkCandidate("chunk one", 1, 1, [1], [], 2), ChunkCandidate("chunk two", 2, 2, [2], [], 2)
        )

    uploads = []

    def flaky_upload(*args, **kwargs):
        uploads.append(args)
        if len(uploads) == 1:
            raise RuntimeError("forced upload failure")
        return ("minio://documents/fake/chunks.json", 2, "application/json")

    _checkpointed(db_connection, monkeypatch, "wf", ["page one content", "page two content"], chunk_pages)
    monkeypatch.setattr(minio_storage, "upload_artifact", flaky_upload)
    with pytest.raises(RuntimeError, match="forced upload failure"):
        await activities.create_chunks_from_db("wf")

    assert (await activities.create_chunks_from_db("wf"))["chunk_count"] == 2
    assert seen == [[1, 2], []]


async def test_checkpoint_finalize_applies_chunk_limit_guards(db_connection, monkeypatch, minio_uploads):
    """Checkpoint rows are raw provider candidates, so finalize must enforce #115."""
    from pipeline.chunking.enforce_limits import (
        MARQO_MAX_DOCUMENT_BYTES,
        enforce_chunk_limits,
        estimate_marqo_tensor_payload_bytes,
    )

    # One window the grouper merged into a single chunk far above both guards.
    oversized = "sentence about cattle feed. " * 6000
    assert estimate_marqo_tensor_payload_bytes(oversized) > MARQO_MAX_DOCUMENT_BYTES

    async def chunk_pages(pages, config, progress_callback=None):
        # Like chunking.service.chunk_pages: raw candidates go out through the
        # callback, and enforcement only touches the return value.
        await progress_callback(_window(1, _window_chunk(oversized, 1, token_count=40000)))
        return enforce_chunk_limits(_result(config, ChunkCandidate(oversized, 1, 1, [1], [], 40000)))

    cfg = _checkpointed(db_connection, monkeypatch, "wf", ["page one"], chunk_pages, max_chunk_tokens=450)
    result = await activities.create_chunks_from_db("wf")

    stored = _stored(db_connection, "wf")
    assert len(stored) > 1, "oversized checkpoint chunk should have been split at finalize"
    assert all(int(c["token_count"] or 0) <= cfg.max_chunk_tokens for c in stored)
    assert all(estimate_marqo_tensor_payload_bytes(c["original_text"]) <= MARQO_MAX_DOCUMENT_BYTES for c in stored)
    assert [c["chunk_number"] for c in stored] == list(range(1, len(stored) + 1))
    assert result["chunk_count"] == len(stored)
    assert all(json.loads(c["source_page_numbers_json"]) == [1] for c in stored)


# --- routes: the job row exists before the workflow that is bound to it starts ------


async def test_retry_chunking_creates_job_before_workflow_start(monkeypatch):
    from pipeline.routers import documents_actions as action_routes

    call_order: list[str] = []

    def create_job(**_kwargs):
        call_order.append("create_job")
        return 321

    async def start_chunking_retry(**kwargs):
        call_order.append("start_workflow")
        assert kwargs["args"][-1] == 321

    monkeypatch.setattr(action_routes.access, "require_document_for_user", lambda workflow_id, user, permission: {
        "workflow_id": workflow_id, "document_id": "doc-1", "filename": "doc.pdf",
        "instance": "tenant-a", "page_count": 12,
    })
    for name, fake in {
        "get_pages": lambda _workflow_id: [{"page_number": 1}],
        "create_document_job": create_job,
        "update_document_fields": lambda *_args, **_kwargs: None,
        "update_document_job": lambda *_args, **_kwargs: None,
        "log_audit": lambda **_kwargs: None,
    }.items():
        monkeypatch.setattr(action_routes.db, name, fake)
    monkeypatch.setattr(action_routes.workflow_runtime, "start_chunking_retry", start_chunking_retry)

    result = await action_routes.retry_chunking("wf-1", object())
    assert result["status"] == "started"
    assert call_order == ["create_job", "start_workflow"]


def _start_document_workflow(monkeypatch, start_document_pipeline, **db_fakes):
    """``start_document_workflow`` with everything but the job row and the workflow
    start stubbed out; returns a call that runs it for ``wf-1``."""
    from pipeline.auth.models import local_bypass_user
    from pipeline.models import RegisterRequest
    from pipeline.routers import documents as doc_routes

    async def dedup_or_none(_user, workflow_id, **_kwargs):
        return None, workflow_id

    monkeypatch.setattr(doc_routes.access, "resolve_create_instance", lambda _user, _instance: "tenant-a")
    monkeypatch.setattr(doc_routes.source_files, "validate_file_path", lambda path: path)
    monkeypatch.setattr(doc_routes.source_files, "get_filename_from_path", lambda _path: "doc.pdf")
    monkeypatch.setattr(doc_routes.source_files, "compute_file_fingerprint", lambda _path: "fp1")
    monkeypatch.setattr(doc_routes.document_service, "dedup_or_none", dedup_or_none)
    monkeypatch.setattr(doc_routes.workflow_runtime, "get_workflow_id", lambda _path: "wf-1")
    monkeypatch.setattr(doc_routes.workflow_runtime, "tenant_workflow_id", lambda workflow_id, _instance: workflow_id)
    monkeypatch.setattr(doc_routes.workflow_runtime, "start_document_pipeline", start_document_pipeline)
    db_fakes = {"upsert_document": lambda **_kw: None, "update_document_fields": lambda *_a, **_kw: None, **db_fakes}
    for name, fake in db_fakes.items():
        monkeypatch.setattr(doc_routes.db, name, fake)
    route = getattr(doc_routes.start_document_workflow, "__wrapped__", doc_routes.start_document_workflow)
    return lambda: route(object(), RegisterRequest(filepath="/data/documents/doc.pdf"), local_bypass_user())


async def test_start_document_workflow_creates_job_before_workflow_start(monkeypatch):
    call_order: list[str] = []

    def create_job(**_kwargs):
        call_order.append("create_job")
        return 321

    async def start_document_pipeline(**kwargs):
        call_order.append("start_workflow")
        assert kwargs["args"][-1] == 321

    run = _start_document_workflow(monkeypatch, start_document_pipeline, create_document_job=create_job)
    assert (await run()).workflow_id == "wf-1"
    assert call_order == ["create_job", "start_workflow"]


async def test_start_document_workflow_marks_job_failed_when_temporal_start_fails(monkeypatch):
    job_updates: list[tuple[int, dict]] = []
    field_updates: list[tuple[str, dict]] = []

    async def start_document_pipeline(**_kwargs):
        raise RuntimeError("temporal down")

    run = _start_document_workflow(
        monkeypatch, start_document_pipeline,
        create_document_job=lambda **_kwargs: 321,
        update_document_job=lambda job_id, **kwargs: job_updates.append((job_id, kwargs)),
        update_document_fields=lambda workflow_id, **kwargs: field_updates.append((workflow_id, kwargs)),
    )
    with pytest.raises(RuntimeError, match="temporal down"):
        await run()

    assert job_updates and job_updates[-1][0] == 321
    assert job_updates[-1][1]["status"] == "failed"
    assert "temporal down" in (job_updates[-1][1].get("error_message") or "")
    assert field_updates
    assert field_updates[-1][0] == "wf-1"
    assert field_updates[-1][1]["latest_job_id"] == 321
