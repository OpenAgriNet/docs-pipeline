"""DEV-only prepared-import publication, verification and deterministic retries."""

import asyncio
from datetime import datetime, timezone
import json
import os

from fastapi import HTTPException
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from .. import db, import_state
from ..auth.permissions import Permission
from ..ingestion_records import prepare_ingestion_records
from ..vector_store import get_vector_store


def dev_target(instance):
    """Only an explicitly configured tenant-owned index in a DEV deployment."""
    if os.environ.get("CHUNK_IMPORT_DEPLOYMENT", "").strip().lower() != "dev":
        raise ValueError("Prepared imports require CHUNK_IMPORT_DEPLOYMENT=dev")
    try:
        mapping = json.loads(os.environ.get("CHUNK_IMPORT_DEV_TARGETS", "{}"))
        logical = mapping.get(instance) if isinstance(mapping, dict) else None
    except ValueError as exc:
        raise ValueError("Invalid CHUNK_IMPORT_DEV_TARGETS configuration") from exc
    if not isinstance(logical, str) or not logical:
        raise ValueError("No DEV import destination configured for this tenant")
    row = db.get_index(instance, logical)
    if not row:
        raise ValueError("DEV import destination must be a registered tenant index")
    return {"logical_index": row["name"], "physical_index": row["marqo_index"],
            "backend_url": get_vector_store().url}


def assert_pinned_target(doc, pub, *, physical=None):
    configured = dev_target(doc["instance"])
    if any(configured[key] != pub[key] for key in configured):
        raise ValueError("Pinned DEV import destination changed; refusing to redirect")
    if physical is not None and physical != pub["physical_index"]:
        raise ValueError("Prepared import cannot use a caller-selected index")


def check_index(target, records=None):
    store = get_vector_store(url=target["backend_url"])
    report = store.describe_index(target["physical_index"])
    if not report.exists or not report.has_passage_tensor:
        raise ValueError("DEV import index must already exist with a passage tensor")
    required = {"workflow_id", "instance", "doc_id", "chunk_num", "text", "is_reference", "domain_tags"}
    if records:
        required |= {field for record in records for field in record if field != "_id"}
    if required - set(report.field_names):
        raise ValueError("DEV import index cannot preserve the prepared chunk metadata")
    return store, report


def _same_value(actual, expected):
    # Structured backends may omit empty optional strings and return an integer
    # representation of a float-valued zero. Never coerce boolean flags/text.
    if expected == "" and actual is None:
        return True
    if type(actual) in {int, float} and type(expected) in {int, float}:
        return actual == expected
    return type(actual) is type(expected) and actual == expected


def recover_upload(user, instance, document_id, fingerprint):
    """Reposting after a crashed upload recovers only its journalled new keys."""
    pending = import_state.active_upload(instance, document_id, fingerprint)
    if not pending:
        return
    if pending["actor"] != user.user_id and Permission.ADMIN not in user.permissions_in(instance):
        raise ValueError("Another user's unfinished upload needs administrator recovery")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(pending["created_at"])).total_seconds()
    if pending["status"] == "uploading" and age < 300:
        raise ValueError("An upload is in progress; retry after its recovery grace period")
    from ..storage import minio as storage
    with import_state.document_lock(pending["workflow_id"]):
        # A publisher may have committed since the first read. Never delete a
        # managed document's objects, regardless of journal age.
        if db.get_document(pending["workflow_id"]):
            raise ValueError("Upload already has a managed document; retry its ingestion instead")
        errors = []
        for bucket, key in json.loads(pending["objects_json"]):
            try:
                storage.delete_object(bucket, key)
            except Exception as exc:
                errors.append(str(exc))
        import_state.finish_upload(pending["workflow_id"], error="; ".join(errors) if errors else None)
        if errors:
            raise ValueError("Unfinished upload cleanup failed; no new import was created")


def publish(workflow_id, document_id, filename, physical, attempt, heartbeat=None):
    """Idempotent activity body. Scope every purge to this new workflow only.

    A shared DEV index can expose in-flight records to direct backend readers;
    there is no atomic multi-record visibility guarantee. SQL reports success
    only after readback. Failures purge partial records, or record cleanup_failed.
    """
    with import_state.document_lock(workflow_id):
        doc, pub = db.get_document(workflow_id), import_state.publication(workflow_id)
        if not doc or not pub or pub["attempt_id"] != attempt:
            raise ValueError("Stale or unbound import attempt")
        if pub["status"] == "verified":
            return {"records_ingested": len(db.get_chunks(workflow_id)), "verified": True}
        store = None
        touched = False
        try:
            assert_pinned_target(doc, pub, physical=physical)
            if doc["document_id"] != document_id or doc["filename"] != filename or doc["is_disabled"]:
                raise ValueError("Import document identity or enablement changed")
            if import_state.manifest(workflow_id) != pub["manifest_hash"]:
                raise ValueError("Prepared chunks changed after this attempt was queued")
            chunks = db.get_chunks(workflow_id, include_excluded=True)
            records = prepare_ingestion_records(document_id, filename, chunks, workflow_id=workflow_id, instance=doc["instance"])
            records = [{key: value for key, value in record.items() if value is not None} for record in records]
            if not records:
                raise ValueError("Import needs at least one non-excluded chunk")
            store, _ = check_index(pub, records)
            import_state.mark(workflow_id, attempt, "indexing", cleanup_required=True)
            touched = True
            purged = store.delete_document(document_id, physical, workflow_id=workflow_id)
            if purged.get("error"):
                raise ValueError(purged["error"])
            def batch_done(errors, response):
                if heartbeat:
                    heartbeat({"phase": "prepared_import_write", "workflow_id": workflow_id})
                if errors:
                    raise ValueError(f"Prepared import batch rejected {len(errors)} record(s)")
            store.add_documents(physical, records, batch_size=10, on_batch=batch_done)
            # Bounded ID reads avoid the search-offset ceiling at exactly
            # 10,000 chunks. The prior scoped purge excludes stale attempt IDs.
            for offset in range(0, len(records), 100):
                if heartbeat:
                    heartbeat({"phase": "prepared_import_verify", "verified": offset})
                expected = {record["_id"]: record for record in records[offset:offset + 100]}
                found = store.get_documents(physical, list(expected))
                if {record.get("_id") for record in found} != set(expected) or len(found) != len(expected):
                    raise ValueError("Imported record IDs failed readback verification")
                for record in found:
                    wanted = expected[record["_id"]]
                    actual = {key: record.get(key) for key in wanted}
                    mismatched = [key for key in wanted if not _same_value(actual[key], wanted[key])]
                    if mismatched:
                        raise ValueError("Imported record metadata failed readback verification: " + ", ".join(mismatched))
            assert_pinned_target(db.get_document(workflow_id), pub, physical=physical)
            import_state.complete(workflow_id, attempt, len(records))
            return {"records_ingested": len(records), "verified": True}
        except Exception as exc:
            cleanup_error = None
            if touched and store is not None:
                try:
                    removed = store.delete_document(document_id, physical, workflow_id=workflow_id)
                    if removed.get("error"):
                        raise ValueError(removed["error"])
                except Exception as cleanup:
                    cleanup_error = str(cleanup)
            uncertain = bool(cleanup_error or (pub["cleanup_required"] and not touched))
            error = str(exc) + (f"; partial-record cleanup failed: {cleanup_error}" if cleanup_error else "")
            if uncertain and not cleanup_error:
                error += "; records from a previous attempt still need confirmed cleanup"
            import_state.mark(workflow_id, attempt, "cleanup_failed" if uncertain else "failed", error,
                              cleanup_required=uncertain)
            db.upsert_document_index_status(workflow_id, physical, status="index_failed",
                chunk_count_indexed=0, details={"error": error, "cleanup_confirmed": not uncertain})
            raise RuntimeError(error) from exc


async def start_or_retry(workflow_id):
    from . import workflow_runtime
    from ..temporal import client as temporal_client
    with import_state.document_lock(workflow_id):
        doc, pub = db.get_document(workflow_id), import_state.publication(workflow_id)
        if not pub:
            raise ValueError("Import has no pinned DEV destination")
        assert_pinned_target(doc, pub)
        # An explicit retry verifies/republishes again, even after an earlier
        # success: records could have been removed outside this pipeline.
        if pub["attempt_id"]:
            job = db.get_document_job(pub["job_id"])
            try:
                info = await (await temporal_client.get_client()).get_workflow_handle(job["temporal_workflow_id"]).describe()
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
                # Restart the SAME deterministic run if its start was lost. A
                # late original RPC cannot create a second run with this ID.
                info = None
            if info and info.status == WorkflowExecutionStatus.RUNNING:
                raise ValueError("This import attempt is still running")
            if info is None and pub["status"] not in {"queued", "start_unknown"}:
                raise ValueError("Previous import execution is missing; recovery needs operator review")
            if info is not None:
                pub = import_state.reserve_attempt(workflow_id)
        else:
            pub = import_state.reserve_attempt(workflow_id)
        job = db.get_document_job(pub["job_id"])
        if pub["status"] == "start_unknown":
            import_state.mark(workflow_id, pub["attempt_id"], "queued")
    try:
        await workflow_runtime.start_job(
            workflow_id, workflow_runtime.start_reingestion,
            args=[doc["document_id"], doc["filename"], workflow_id, doc["page_count"], doc["chunk_count"],
                  pub["physical_index"], pub["attempt_id"]],
            temporal_workflow_id=job["temporal_workflow_id"], instance=doc["instance"],
            job_type="prepared_import", stage="ingesting", config=json.loads(job["config_json"]),
            bind_job_id=False, reserved_job_id=pub["job_id"],
        )
    except WorkflowAlreadyStartedError:
        # A repeated start with the same ID is an idempotent acknowledgement.
        pass
    except Exception as exc:
        current = import_state.publication(workflow_id)
        if current["status"] != "verified":
            if current["status"] == "queued":
                import_state.mark(workflow_id, pub["attempt_id"], "start_unknown", str(exc))
            raise HTTPException(503, {"message": "Import saved; Temporal start is unconfirmed", "workflow_id": workflow_id,
                                      "retry_url": f"/documents/{workflow_id}/retry-import"}) from exc
    current = import_state.publication(workflow_id)
    # A start response can be lost after the worker already committed success;
    # shared start_job's error reporting must not leave that verified job failed.
    if current["status"] in {"verified", "queued", "indexing"}:
        db.update_document_job(current["job_id"], status="completed" if current["status"] == "verified" else "running",
                               current_stage="completed" if current["status"] == "verified" else "ingesting",
                               error_message=None)
        if current["status"] == "verified":
            db.update_document_fields(workflow_id, error_message=None)
    return current


async def refresh_status(workflow_id):
    """A crashed worker must not leave the API claiming an eternal running job."""
    from ..temporal import client as temporal_client
    pub = import_state.publication(workflow_id)
    if not pub or not pub["attempt_id"] or pub["status"] not in {"queued", "indexing", "start_unknown"}:
        return pub
    job = db.get_document_job(pub["job_id"])
    try:
        info = await (await temporal_client.get_client()).get_workflow_handle(job["temporal_workflow_id"]).describe()
    except Exception:
        return {**pub, "runtime_unavailable": True}  # Keep the last known state.
    if info.status != WorkflowExecutionStatus.RUNNING:
        with import_state.document_lock(workflow_id):
            latest = import_state.publication(workflow_id)
            if latest["attempt_id"] == pub["attempt_id"] and latest["status"] != "verified":
                import_state.mark(workflow_id, pub["attempt_id"],
                    "cleanup_failed" if pub["status"] == "indexing" else "failed",
                    "Workflow ended without verified publication; retry to clean and republish",
                    cleanup_required=True if pub["status"] == "indexing" else None)
    return import_state.publication(workflow_id)
