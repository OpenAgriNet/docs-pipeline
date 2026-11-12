"""DEV-only import orchestration with real SQLite, ASGI and adapter contracts."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from pipeline import import_state
from pipeline.auth import deps
from pipeline.services import import_jobs
from pipeline.services.chunk_import import create_prepared_import
from pipeline.temporal import document_tasks
from tests.helpers import member, seed_document
from tests.test_chunk_import import Objects, pdf


TARGET = "t-a-import_dev"


@pytest.fixture
def imports(test_client, db_connection, fake_marqo, monkeypatch, mock_temporal_client):
    from pipeline.rate_limit import limiter
    monkeypatch.setattr(limiter, "enabled", False)
    db_connection.create_tenant_row("a")
    db_connection.create_index_row("a", "import_dev", TARGET, is_default=False)
    fake_marqo.add_index(TARGET)
    monkeypatch.setenv("CHUNK_IMPORT_DEPLOYMENT", "dev")
    monkeypatch.setenv("CHUNK_IMPORT_DEV_TARGETS", '{"a":"import_dev"}')
    monkeypatch.setenv("AUTH_DISABLED", "false")
    users = {"curator": member("a", "content_curator"), "admin": member("a", "admin"),
             "viewer": member("a", "viewer"), "foreign": member("b", "admin"),
             "mixed": member({"a": ["viewer"], "b": ["admin"]})}
    monkeypatch.setattr(deps, "decode_and_validate_token", lambda token, cfg: users[token])
    test_client.headers["Authorization"] = "Bearer curator"
    objects = Objects()
    monkeypatch.setattr("pipeline.storage.minio.get_client", lambda: objects)
    mock_temporal_client.get_workflow_handle.return_value.describe = AsyncMock(
        return_value=SimpleNamespace(status=WorkflowExecutionStatus.FAILED))
    return SimpleNamespace(api=test_client, db=db_connection, store=fake_marqo,
                           objects=objects, temporal=mock_temporal_client, users=users)


def upload(env, rows=None, **fields):
    data = {"document_id": "new", "filename": "Prepared notes.pdf", "instance": "a", **fields}
    content = json.dumps(rows or [{"chunk_number": 3, "text": "Milk guidance", "token_count": 3,
                                  "is_reference": True, "domain_tags": ["species:cattle"]}]).encode()
    return env.api.post("/documents/import-chunks", data=data,
                        files={"chunks": ("chunks.json", content, "application/json")})


def publish(env, workflow_id):
    doc, pub = env.db.get_document(workflow_id), import_state.publication(workflow_id)
    return asyncio.run(document_tasks.ingest_document_from_db(
        workflow_id, doc["document_id"], doc["filename"], pub["physical_index"], 10, pub["attempt_id"]))


def test_api_queues_only_reingestion_and_completes_after_exact_readback(imports):
    env = imports
    response = upload(env)
    assert response.status_code == 202, response.text
    workflow = response.json()["workflow_id"]
    doc = env.db.get_document(workflow)
    assert doc["filename"] == "Prepared notes.pdf" and not doc["query_enabled"]
    assert import_state.publication(workflow)["status"] == "queued"
    assert not env.store.index(TARGET).records
    args = env.temporal.start_workflow.call_args.kwargs["args"]
    assert args[0:3] == ["new", "Prepared notes.pdf", workflow]
    assert args[5] == TARGET and len(args) == 7
    assert len(env.db.list_document_jobs(workflow)) == 1
    result = publish(env, workflow)
    assert result == {"records_ingested": 1, "verified": True}
    status = env.api.get(response.json()["status_url"]).json()
    assert status["stage"] == "completed" and status["import_status"] == "verified" and status["query_enabled"]
    records = env.store.index(TARGET).records
    assert records[0]["is_reference"] is True and records[0]["domain_tags"] == "|species:cattle|"
    assert env.db.get_latest_document_job(workflow)["status"] == "completed"
    assert publish(env, workflow) == result  # Lost activity response: readback is idempotent.
    assert len(env.store.index(TARGET).records) == 1
    assert env.db.get_pages(workflow) == []
    assert env.api.get(f"/documents/{workflow}/pdf").status_code == 404  # Filename alone is not a PDF.


@pytest.mark.parametrize("token,status", [("viewer",403), ("foreign",404), ("mixed",403)])
def test_upload_permission_is_checked_in_selected_tenant(imports, token, status):
    imports.api.headers["Authorization"] = f"Bearer {token}"
    response = upload(imports)
    assert response.status_code == status, response.text
    assert not imports.objects.uploads and not imports.temporal.start_workflow.called


@pytest.mark.parametrize("key,value", [("CHUNK_IMPORT_DEPLOYMENT", ""), ("CHUNK_IMPORT_DEPLOYMENT", "prod"),
    ("CHUNK_IMPORT_DEV_TARGETS", "{}"), ("CHUNK_IMPORT_DEV_TARGETS", "bad json"),
    ("CHUNK_IMPORT_DEV_TARGETS", '{"a":"missing"}')])
def test_missing_or_invalid_dev_target_has_zero_writes(imports, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    response = upload(imports)
    assert response.status_code == 503, response.text
    assert not imports.objects.uploads and not imports.temporal.start_workflow.called
    assert not imports.db.list_documents(instances=["a"])


def test_default_is_usable_only_when_explicitly_configured_as_dev_target(imports, monkeypatch):
    imports.db.create_index_row("a", "default", "t-a-default", is_default=True)
    row = imports.db.get_default_index("a")
    imports.store.add_index(row["marqo_index"])
    monkeypatch.setenv("CHUNK_IMPORT_DEV_TARGETS", json.dumps({"a": row["name"]}))
    response = upload(imports)
    assert response.status_code == 202, response.text
    assert import_state.publication(response.json()["workflow_id"])["physical_index"] == "t-a-default"


def test_invalid_and_all_excluded_files_have_no_storage_or_job_effects(imports):
    for row in [{"chunk_number": 1, "text": " "}, {"chunk_number": 1, "text": "x", "is_excluded": True}]:
        response = upload(imports, [row])
        assert response.status_code == 422, response.text
    assert not imports.objects.uploads and not imports.temporal.start_workflow.called


def test_foreign_status_retry_and_mutations_do_not_leak_or_change_import(imports):
    workflow = upload(imports).json()["workflow_id"]
    imports.api.headers["Authorization"] = "Bearer foreign"
    for method, path in [("get", "import-status"), ("post", "retry-import")]:
        assert getattr(imports.api, method)(f"/documents/{workflow}/{path}").status_code == 404
    imports.api.headers["Authorization"] = "Bearer admin"
    assert imports.api.patch(f"/documents/{workflow}/chunks/3", json={"edited_text":"racing edit"}).status_code == 409
    assert imports.api.post(f"/documents/{workflow}/query-enabled", json={"query_enabled":True}).status_code == 409
    assert imports.db.get_chunks(workflow)[0]["original_text"] == "Milk guidance"


@pytest.mark.parametrize("change", ["registry", "endpoint", "environment"])
def test_worker_fails_closed_if_pinned_destination_drifts(imports, monkeypatch, change):
    workflow = upload(imports).json()["workflow_id"]
    if change == "endpoint":
        monkeypatch.setenv("MARQO_URL", "http://wrong-endpoint.invalid")
    elif change == "environment":
        monkeypatch.setenv("CHUNK_IMPORT_DEPLOYMENT", "prod")
    else:
        monkeypatch.setenv("CHUNK_IMPORT_DEV_TARGETS", '{"a":"other"}')
    with pytest.raises(RuntimeError, match="DEV|destination|redirect|DEPLOYMENT"):
        publish(imports, workflow)
    assert not imports.store.index(TARGET).records
    assert not imports.db.get_document(workflow)["query_enabled"]
    assert import_state.publication(workflow)["status"] == "failed"


def test_partial_batch_failure_cleans_only_this_workflow_then_retry_succeeds(imports):
    env = imports
    rows = [{"chunk_number": n, "text": f"Row {n}", "token_count": 2} for n in range(1, 23)]
    workflow = upload(env, rows).json()["workflow_id"]
    index = env.store.index(TARGET)
    sentinel = {"_id":"keep", "workflow_id":"other", "doc_id":"other", "text":"Existing content"}
    index.records.append(sentinel)
    index.fail("add_documents", RuntimeError("second batch failed"), on_call=2)
    with pytest.raises(RuntimeError, match="second batch"):
        publish(env, workflow)
    assert index.records == [sentinel]
    assert import_state.publication(workflow)["status"] == "failed"
    previous_job = env.db.get_latest_document_job(workflow)["id"]
    index._faults.clear()
    response = env.api.post(f"/documents/{workflow}/retry-import")
    assert response.status_code == 202, response.text
    assert env.db.get_latest_document_job(workflow)["id"] != previous_job
    assert publish(env, workflow)["records_ingested"] == 22
    assert len(index.records) == 23 and sentinel in index.records


def test_wrong_metadata_readback_cannot_be_completed(imports, monkeypatch):
    workflow = upload(imports).json()["workflow_id"]
    index = imports.store.index(TARGET)
    read = index.get_documents
    def corrupt(*args, **kwargs):
        result = read(*args, **kwargs)
        result["results"][0]["is_reference"] = False
        return result
    monkeypatch.setattr(index, "get_documents", corrupt)
    with pytest.raises(RuntimeError, match="metadata"):
        publish(imports, workflow)
    assert not index.records and not imports.db.get_document(workflow)["query_enabled"]


def test_start_timeout_keeps_doc_and_restarts_same_attempt(imports):
    env = imports
    env.temporal.start_workflow.side_effect = TimeoutError("reply lost")
    response = upload(env)
    assert response.status_code == 503, response.text
    workflow = response.json()["detail"]["workflow_id"]
    initial = import_state.publication(workflow)
    assert initial["status"] == "start_unknown" and not env.store.index(TARGET).records
    env.temporal.start_workflow.side_effect = None
    env.temporal.get_workflow_handle.return_value.describe.side_effect = RPCError("missing", RPCStatusCode.NOT_FOUND, b"")
    response = env.api.post(f"/documents/{workflow}/retry-import")
    assert response.status_code == 202, response.text
    current = import_state.publication(workflow)
    assert current["job_id"] == initial["job_id"] and current["attempt_id"] == initial["attempt_id"]
    assert publish(env, workflow)["verified"]


def test_running_temporal_attempt_cannot_be_duplicated(imports):
    workflow = upload(imports).json()["workflow_id"]
    imports.temporal.get_workflow_handle.return_value.describe.return_value.status = WorkflowExecutionStatus.RUNNING
    assert imports.api.post(f"/documents/{workflow}/retry-import").status_code == 409
    assert len(imports.db.list_document_jobs(workflow)) == 1


def test_crashed_upload_reposting_recovers_only_journalled_keys(imports):
    env = imports
    content = json.dumps([{"chunk_number":3,"text":"Milk guidance","token_count":3,
                           "is_reference":True,"domain_tags":["species:cattle"]}]).encode()
    key = ("documents", "a/dead-upload/original_upload/chunks.json")
    env.objects.objects[key] = content
    env.objects.objects[("documents", "unrelated.pdf")] = b"keep"
    import_state.begin_upload("dead-upload", "a", "member", "new", hashlib.sha256(content).hexdigest(), [key])
    with env.db.get_connection() as conn, conn:
        conn.execute("UPDATE prepared_uploads SET created_at = ? WHERE workflow_id = 'dead-upload'",
                     ((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),))
    response = upload(env)
    assert response.status_code == 202, response.text
    assert key not in env.objects.objects and ("documents","unrelated.pdf") in env.objects.objects


def test_untracked_prepared_document_cannot_use_generic_ingestion(imports):
    doc = create_prepared_import(user=imports.users["curator"], instance="a", document_id="unbound",
                                filename="chunks.json", content=b'[{"chunk_number":1,"text":"x","token_count":1}]', file_format="json")
    with pytest.raises(ValueError, match="tracked DEV"):
        asyncio.run(document_tasks.ingest_document_from_db(doc["workflow_id"], "unbound", "chunks.json", TARGET))
    assert not imports.store.index(TARGET).records


def test_duplicate_request_returns_existing_receipt_without_overwriting(imports):
    first = upload(imports).json()
    again = upload(imports)
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["workflow_id"] == first["workflow_id"] and detail["same_file"]
    changed = upload(imports, [{"chunk_number":3,"text":"Different content"}])
    assert changed.status_code == 409 and changed.json()["detail"]["retry_url"] is None
    assert len(imports.objects.objects) == 1 and len(imports.db.list_document_jobs(first["workflow_id"])) == 1


def test_cleanup_failure_is_visible_and_never_enables_queries(imports, monkeypatch):
    workflow = upload(imports).json()["workflow_id"]
    index = imports.store.index(TARGET)
    index.fail("get_documents", RuntimeError("readback unavailable"))
    original = index.delete_documents
    monkeypatch.setattr(index, "delete_documents", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("delete failed")))
    with pytest.raises(RuntimeError, match="cleanup failed"):
        publish(imports, workflow)
    assert import_state.publication(workflow)["status"] == "cleanup_failed"
    assert index.records and not imports.db.get_document(workflow)["query_enabled"]
    imports.api.headers["Authorization"] = "Bearer admin"
    assert imports.api.post(f"/documents/{workflow}/query-enabled", json={"query_enabled":True}).status_code == 409
    monkeypatch.setattr(index, "delete_documents", original)
    index._faults.clear()
    assert imports.api.post(f"/documents/{workflow}/retry-import").status_code == 202
    assert publish(imports, workflow)["verified"] and len(index.records) == 1


def test_successful_import_can_repair_records_removed_outside_pipeline(imports):
    workflow = upload(imports).json()["workflow_id"]
    publish(imports, workflow)
    imports.store.index(TARGET).records.clear()
    old = import_state.publication(workflow)
    response = imports.api.post(f"/documents/{workflow}/retry-import")
    assert response.status_code == 202, response.text
    assert import_state.publication(workflow)["attempt_id"] != old["attempt_id"]
    publish(imports, workflow)
    assert len(imports.store.index(TARGET).records) == 1


def test_terminal_worker_crash_is_reported_without_faking_verification(imports):
    workflow = upload(imports).json()["workflow_id"]
    pub = import_state.publication(workflow)
    import_state.mark(workflow, pub["attempt_id"], "indexing")
    response = imports.api.get(f"/documents/{workflow}/import-status")
    assert response.status_code == 200 and response.json()["import_status"] == "cleanup_failed"
    assert response.json()["stage"] == "failed" and not response.json()["query_enabled"]
    assert imports.db.get_latest_document_job(workflow)["status"] == "failed"


def test_temporal_outage_is_not_a_failed_execution(imports):
    workflow = upload(imports).json()["workflow_id"]
    imports.temporal.get_workflow_handle.return_value.describe.side_effect = ConnectionError("offline")
    response = imports.api.get(f"/documents/{workflow}/import-status")
    assert response.json()["import_status"] == "queued"
    assert response.json()["runtime_unavailable"]
    assert len(imports.db.list_document_jobs(workflow)) == 1
    previous = import_state.publication(workflow)
    assert imports.api.post(f"/documents/{workflow}/retry-import").status_code == 503
    assert import_state.publication(workflow)["attempt_id"] == previous["attempt_id"]


def test_stale_attempt_cannot_mutate_a_newer_run(imports):
    workflow = upload(imports).json()["workflow_id"]
    old = import_state.publication(workflow)
    current = import_state.reserve_attempt(workflow)
    doc = imports.db.get_document(workflow)
    with pytest.raises(ValueError, match="Stale"):
        import_jobs.publish(workflow, doc["document_id"], doc["filename"], TARGET, old["attempt_id"])
    assert import_state.publication(workflow)["attempt_id"] == current["attempt_id"]
    assert not imports.store.index(TARGET).records


def test_source_change_during_write_prevents_completion(imports, monkeypatch):
    workflow = upload(imports).json()["workflow_id"]
    index = imports.store.index(TARGET)
    original = index.get_documents
    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        imports.db.update_chunk(workflow, 3, edited_text="Changed through a non-API writer")
        return result
    monkeypatch.setattr(index, "get_documents", mutate)
    with pytest.raises(RuntimeError, match="changed"):
        publish(imports, workflow)
    assert not index.records and not imports.db.get_document(workflow)["query_enabled"]


def test_10000_imported_records_are_verified_without_search_offset_limits(imports):
    rows = [{"chunk_number": n, "text": f"Row {n}", "token_count": 2} for n in range(1, 10_001)]
    response = upload(imports, rows)
    assert response.status_code == 202, response.text
    workflow = response.json()["workflow_id"]
    assert publish(imports, workflow)["records_ingested"] == 10_000
    index = imports.store.index(TARGET)
    assert index._calls["get_documents"] == 100
    assert len(index.records) == 10_000


@pytest.mark.parametrize("fresh,locked", [(True,False), (False,True)])
def test_recovery_never_removes_an_active_upload(imports, fresh, locked):
    key = ("documents", "a/live/original_upload/chunks.json")
    imports.objects.objects[key] = b"in progress"
    import_state.begin_upload("live", "a", "member", "new", "hash", [key])
    if not fresh:
        with imports.db.get_connection() as conn, conn:
            conn.execute("UPDATE prepared_uploads SET created_at = ?", ((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),))
    if locked:
        with import_state.document_lock("live"), pytest.raises(ValueError, match="running"):
            import_jobs.recover_upload(imports.users["curator"], "a", "new", "hash")
    else:
        with pytest.raises(ValueError, match="in progress"):
            import_jobs.recover_upload(imports.users["curator"], "a", "new", "hash")
    assert key in imports.objects.objects


def test_upload_limit_is_enforced_during_read(imports, monkeypatch):
    from pipeline.services import chunk_import
    from dataclasses import replace
    real = chunk_import.ImportLimits()
    monkeypatch.setattr(chunk_import, "ImportLimits", lambda: replace(real, max_upload_bytes=8))
    response = upload(imports)
    assert response.status_code == 413 and not imports.objects.uploads
    assert not imports.temporal.start_workflow.called


def test_cleanup_obligation_survives_next_attempt_failing_before_write(imports, monkeypatch):
    workflow = upload(imports).json()["workflow_id"]
    index = imports.store.index(TARGET)
    delete = index.delete_documents
    index.fail("get_documents", RuntimeError("readback failed"))
    monkeypatch.setattr(index, "delete_documents", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cleanup failed")))
    with pytest.raises(RuntimeError):
        publish(imports, workflow)
    assert import_state.publication(workflow)["cleanup_required"]
    index._faults.clear()
    monkeypatch.setattr(index, "delete_documents", delete)
    assert imports.api.post(f"/documents/{workflow}/retry-import").status_code == 202
    index.fail("get_settings", RuntimeError("schema temporarily unavailable"))
    with pytest.raises(RuntimeError, match="previous attempt"):
        publish(imports, workflow)
    pub = import_state.publication(workflow)
    assert pub["status"] == "cleanup_failed" and pub["cleanup_required"]
    assert index.records and not imports.db.get_document(workflow)["query_enabled"]


def test_sql_completion_failure_cleans_written_records(imports):
    workflow = upload(imports).json()["workflow_id"]
    with imports.db.get_connection() as conn, conn:
        conn.execute("""CREATE TRIGGER reject_completion BEFORE UPDATE ON documents
            WHEN NEW.stage = 'completed' BEGIN SELECT RAISE(ABORT, 'commit rejected'); END""")
    with pytest.raises(RuntimeError, match="commit rejected"):
        publish(imports, workflow)
    assert not imports.store.index(TARGET).records
    pub = import_state.publication(workflow)
    assert pub["status"] == "failed" and not pub["cleanup_required"]
    assert imports.db.get_latest_document_job(workflow)["status"] == "failed"


def test_lost_start_response_after_verification_does_not_undo_success(imports):
    async def start_then_lose_reply(*args, **kwargs):
        workflow = kwargs["args"][2]
        doc, pub = imports.db.get_document(workflow), import_state.publication(workflow)
        await document_tasks.ingest_document_from_db(workflow, doc["document_id"], doc["filename"], TARGET, 10, pub["attempt_id"])
        raise TimeoutError("Start acknowledgement lost after successful publication")
    imports.temporal.start_workflow.side_effect = start_then_lose_reply
    response = upload(imports)
    assert response.status_code == 202, response.text
    workflow = response.json()["workflow_id"]
    assert response.json()["import_status"] == "verified"
    assert imports.db.get_latest_document_job(workflow)["status"] == "completed"
    assert imports.db.get_document(workflow)["error_message"] is None


def test_oversized_wire_request_is_rejected_before_multipart_parsing(imports):
    response = imports.api.post("/documents/import-chunks", content=b"small",
                                headers={"Content-Length": str(200 * 1024 * 1024), "Origin":"http://localhost:3000"})
    assert response.status_code == 413 and not imports.objects.uploads
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert not imports.temporal.start_workflow.called


@pytest.mark.asyncio
async def test_import_body_limit_also_applies_under_a_proxy_root(imports):
    import httpx
    from pipeline.app import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, root_path="/api"), base_url="http://test") as client:
        response = await client.post("/api/documents/import-chunks", content=b"small",
                                     headers={"Content-Length":str(200*1024*1024)})
    assert response.status_code == 413 and not imports.objects.uploads


@pytest.mark.asyncio
async def test_chunked_wire_body_cannot_bypass_the_combined_limit(imports, monkeypatch):
    import httpx
    from pipeline.app import app
    from pipeline.upload_limits import ImportBodyLimitMiddleware
    monkeypatch.setattr(ImportBodyLimitMiddleware, "max_body_bytes", 512)
    async def body():
        yield b'--limit\r\nContent-Disposition: form-data; name="chunks"; filename="chunks.json"\r\n\r\n'
        yield b"x" * 1024
        yield b"\r\n--limit--\r\n"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/documents/import-chunks", content=body(),
            headers={"Content-Type":"multipart/form-data; boundary=limit", "Authorization":"Bearer curator"})
    assert response.status_code == 413 and not imports.objects.uploads
    assert not imports.temporal.start_workflow.called


def test_busy_prepared_row_preserves_bulk_helper_skip_contract(imports):
    from pipeline.services.access import document_for_user_or_none
    from pipeline.auth.permissions import Permission
    workflow = upload(imports).json()["workflow_id"]
    seed_document(imports.db, "ordinary", instance="a", chunks=["Ordinary content"])
    assert document_for_user_or_none(workflow, imports.users["curator"], Permission.REVIEW) is None
    assert document_for_user_or_none("ordinary", imports.users["curator"], Permission.REVIEW)["workflow_id"] == "ordinary"
