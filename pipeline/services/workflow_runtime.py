"""Temporal workflow execution, reporting, reconciliation, and approval helpers."""

import asyncio
import hashlib
import json
import logging
import time
from typing import Optional

from fastapi import HTTPException

from .. import db
from ..auth.models import AuthUser
from ..auth.permissions import Permission
from ..auth.tenancy import default_instance, normalize_instance
from ..models import (
    BulkWorkflowActionRequest,
    BulkWorkflowActionResponse,
    BulkWorkflowActionResult,
)
from ..temporal import client as temporal_client
from ..temporal.document_workflows import (
    ChunkingOnlyWorkflow,
    DocumentPipelineWorkflow,
    OcrOnlyWorkflow,
    ReingestionWorkflow,
    TranslationOnlyWorkflow,
)
from ..temporal.failures import get_failure_details
from . import access, progress as live_progress


TASK_QUEUE = temporal_client.TASK_QUEUE


def get_workflow_id(filepath: str) -> str:
    """Generate the stable workflow identifier for a source path."""
    return f"doc-{hashlib.md5(filepath.encode()).hexdigest()[:12]}"


def rerun_workflow_id(base_workflow_id: str) -> str:
    """Generate a fresh workflow identifier for an explicit rerun."""
    return f"{base_workflow_id}-rerun-{int(time.time())}"


def tenant_workflow_id(base_workflow_id: str, instance: str) -> str:
    """Make non-default workflow identifiers collision-safe across tenants."""
    inst = normalize_instance(instance)
    if inst == default_instance():
        return base_workflow_id
    return f"wf-{inst}-{base_workflow_id}"


_instance_search_attr_supported: Optional[bool] = None


async def _start_pipeline_workflow(run, *, args: list, id: str, instance: str):
    """Start a workflow with tenant memo and best-effort search attribute."""
    global _instance_search_attr_supported
    inst = normalize_instance(instance)
    memo = {"instance": inst}

    if _instance_search_attr_supported is not False:
        try:
            handle = await (await temporal_client.get_client()).start_workflow(
                run,
                args=args,
                id=id,
                task_queue=TASK_QUEUE,
                memo=memo,
                search_attributes={"Instance": [inst]},
            )
            _instance_search_attr_supported = True
            return handle
        except Exception as exc:  # noqa: BLE001 - narrowed to search-attribute errors
            message = str(exc).lower()
            if "search attribute" not in message and "searchattribute" not in message:
                raise
            _instance_search_attr_supported = False
            logging.info(
                "Temporal `Instance` search attribute is not registered; "
                "starting with memo only. Register it to enable UI filtering."
            )

    return await (await temporal_client.get_client()).start_workflow(
        run,
        args=args,
        id=id,
        task_queue=TASK_QUEUE,
        memo=memo,
    )


async def start_document_pipeline(*, args: list, id: str, instance: str):
    return await _start_pipeline_workflow(
        DocumentPipelineWorkflow.run, args=args, id=id, instance=instance
    )


async def start_reingestion(*, args: list, id: str, instance: str):
    return await _start_pipeline_workflow(
        ReingestionWorkflow.run, args=args, id=id, instance=instance
    )


async def start_ocr_retry(*, args: list, id: str, instance: str):
    return await _start_pipeline_workflow(
        OcrOnlyWorkflow.run, args=args, id=id, instance=instance
    )


async def start_translation_retry(*, args: list, id: str, instance: str):
    return await _start_pipeline_workflow(
        TranslationOnlyWorkflow.run, args=args, id=id, instance=instance
    )


async def start_chunking_retry(*, args: list, id: str, instance: str):
    return await _start_pipeline_workflow(
        ChunkingOnlyWorkflow.run, args=args, id=id, instance=instance
    )


async def query_workflow_state(workflow_id: str):
    """Query the stable state projection for an existing workflow."""
    handle = (await temporal_client.get_client()).get_workflow_handle(workflow_id)
    return await handle.query("get_state")


async def cancel_workflow_if_running(workflow_id: str) -> bool:
    """Best-effort cancellation used by document disable/delete flows."""
    try:
        handle = (await temporal_client.get_client()).get_workflow_handle(workflow_id)
        await handle.cancel()
        return True
    except Exception:
        return False


async def temporal_is_available() -> bool:
    return await temporal_client.get_client_or_none() is not None


async def get_workflow_error_details(workflow_id: str) -> dict:
    """Return failure details without leaking Temporal SDK types to routers."""
    try:
        handle = (await temporal_client.get_client()).get_workflow_handle(workflow_id)
        description = await handle.describe()
        result = {
            "workflow_id": workflow_id,
            "run_id": description.run_id,
            "status": description.status.name,
            "error_message": None,
            "error_type": None,
            "stack_trace": None,
            "has_error": False,
        }
        if description.status.name == "FAILED":
            result["has_error"] = True
            try:
                failure_details = await get_failure_details(handle)
                if failure_details:
                    result.update(failure_details)
            except Exception as exc:
                result["error_message"] = f"Could not retrieve error details: {exc}"

        if not result["error_message"]:
            try:
                state = await handle.query("get_state")
                if state and state.get("error_message"):
                    result["error_message"] = state.get("error_message")
                    result["has_error"] = True
            except Exception:
                pass
        return result
    except HTTPException:
        raise
    except Exception as exc:
        message = str(exc)
        if "not found" in message.lower() or "workflow" in message.lower():
            raise HTTPException(404, f"Workflow not found: {workflow_id}")
        raise HTTPException(500, f"Error fetching workflow details: {message}")


async def get_runtime_payload(workflow_id: str, doc: Optional[dict] = None) -> dict:
    """Combine SQLite and Temporal state for the document runtime endpoint."""
    doc = doc or db.get_document(workflow_id)
    if not doc:
        raise HTTPException(404, f"Document not found: {workflow_id}")
    current_job = db.get_latest_document_job(workflow_id)
    runtime_workflow_id = (
        current_job.get("temporal_workflow_id")
        if current_job
        and current_job.get("status") == "running"
        and current_job.get("temporal_workflow_id")
        else workflow_id
    )

    chunking_progress = None
    if current_job and current_job.get("config_json"):
        try:
            parsed_config = (
                json.loads(current_job["config_json"])
                if isinstance(current_job["config_json"], str)
                else current_job["config_json"]
            )
            if isinstance(parsed_config, dict):
                chunking_progress = parsed_config.get("chunking_progress")
        except Exception:
            chunking_progress = None

    client = await temporal_client.get_client_or_none()
    runtime = {
        "workflow_id": workflow_id,
        "sqlite_stage": doc.get("stage"),
        "sqlite_error_message": doc.get("error_message"),
        "temporal_connected": client is not None,
        "job": current_job,
        "chunking_progress": chunking_progress,
        "temporal": None,
        "progress": None,
    }
    if client is None:
        return runtime

    description = None
    describe_ok = False
    try:
        handle = client.get_workflow_handle(runtime_workflow_id)
        description = await live_progress.describe_workflow_cached(
            client, runtime_workflow_id, handle=handle
        )
        describe_ok = True
        temporal_state = None
        query_error = None
        try:
            temporal_state = await handle.query("get_state")
        except Exception as exc:
            query_error = str(exc)
        close_time = getattr(description, "close_time", None)
        execution_time = getattr(description, "execution_time", None)
        status = getattr(description, "status", None)
        runtime["temporal"] = {
            "workflow_id": runtime_workflow_id,
            "run_id": getattr(description, "run_id", None),
            "status": getattr(status, "name", None),
            "close_time": close_time.isoformat() if close_time else None,
            "execution_time": execution_time.isoformat() if execution_time else None,
            "state": temporal_state,
            "query_error": query_error,
        }
    except Exception as exc:
        runtime["temporal"] = {
            "workflow_id": workflow_id,
            "status": "UNAVAILABLE",
            "error": str(exc),
        }
    runtime["progress"] = await live_progress.progress_for_runtime(
        workflow_id=workflow_id,
        doc=doc,
        chunking_progress=chunking_progress,
        description=description,
        temporal_connected=True,
        describe_ok=describe_ok,
    )
    return runtime


# Every action reconcile_single_document can return, mapped to the bulk summary
# bucket it belongs to. Keeping the map next to the producer is what stops a new
# action from silently dropping out of the totals.
RECONCILE_OUTCOME_BUCKETS = {
    "materialized_state_reconciled": "updated",
    "stage_synced": "updated",
    "no_change": "still_running",
    "temporal_not_found": "skipped",
    "temporal_unavailable": "skipped",
    "error": "errors",
}


def reconcile_outcome_bucket(action: Optional[str]) -> str:
    """Bucket a reconcile action. Unknown actions count as errors, never vanish."""
    return RECONCILE_OUTCOME_BUCKETS.get(action or "", "errors")


async def reconcile_single_document(doc: dict) -> dict:
    """Reconcile one materialized document against its Temporal execution.

    SQLite lookups and Temporal client construction sit outside the Temporal
    query try, so the whole helper is covered: a per-document fault returns
    ``action=error`` instead of aborting a bulk run.
    """
    workflow_id = doc.get("workflow_id")
    current_stage = doc.get("stage")
    try:
        materialized = db.reconcile_materialized_state(workflow_id)
        if materialized and materialized.get("updated"):
            doc = db.get_document(workflow_id) or doc
            current_stage = doc.get("stage")
            return {
                "workflow_id": workflow_id,
                "action": "materialized_state_reconciled",
                "to": current_stage,
                "page_count": doc.get("page_count", 0),
                "chunk_count": doc.get("chunk_count", 0),
                "job_status": materialized.get("job_status"),
                "job_stage": materialized.get("job_stage"),
            }

        current_job = db.get_latest_document_job(workflow_id)
        runtime_workflow_id = (
            current_job.get("temporal_workflow_id")
            if current_job
            and current_job.get("status") == "running"
            and current_job.get("temporal_workflow_id")
            else workflow_id
        )
        # An unreachable Temporal is an outage, not a fault in this document, so it
        # is reported as a skip like the query-timeout path below rather than as a
        # per-document error.
        client = await temporal_client.get_client_or_none()
        if client is None:
            return {
                "workflow_id": workflow_id,
                "action": "temporal_unavailable",
                "from": current_stage,
                "reason": "temporal_unreachable",
            }

        try:
            handle = client.get_workflow_handle(runtime_workflow_id)
            state = await asyncio.wait_for(handle.query("get_state"), timeout=5.0)
            temporal_stage = state.get("stage") if state else None
            if temporal_stage and temporal_stage != current_stage:
                db.update_document_stage(workflow_id, temporal_stage)
                return {
                    "workflow_id": workflow_id,
                    "action": "stage_synced",
                    "from": current_stage,
                    "to": temporal_stage,
                    "temporal_workflow_id": runtime_workflow_id,
                }
            return {
                "workflow_id": workflow_id,
                "action": "no_change",
                "stage": current_stage,
                "temporal_workflow_id": runtime_workflow_id,
            }
        except asyncio.TimeoutError:
            return {
                "workflow_id": workflow_id,
                "action": "temporal_unavailable",
                "from": current_stage,
                "reason": "query_timeout",
            }
        except Exception as exc:
            error_message = str(exc)
            if "not found" in error_message.lower() or "workflow task" in error_message.lower():
                db.log_audit(
                    workflow_id=workflow_id,
                    document_id=doc.get("document_id", ""),
                    action_type="reconcile_skipped",
                    metadata={"from_stage": current_stage, "reason": "workflow_not_found"},
                )
                return {
                    "workflow_id": workflow_id,
                    "action": "temporal_not_found",
                    "from": current_stage,
                    "reason": "workflow_not_found",
                    "stage": current_stage,
                }
            return {
                "workflow_id": workflow_id,
                "action": "error",
                "from": current_stage,
                "reason": error_message,
            }
    except Exception as exc:
        return {
            "workflow_id": workflow_id,
            "action": "error",
            "from": current_stage,
            "reason": str(exc),
        }


async def validate_approval_stage(workflow_id: str, expected_stage: str):
    """Validate that a workflow is in the expected stage before approval."""
    try:
        handle = (await temporal_client.get_client()).get_workflow_handle(workflow_id)
        state = await handle.query("get_state")
        current_stage = (
            state.get("stage") if isinstance(state, dict) else getattr(state, "stage", None)
        )
        if current_stage != expected_stage:
            raise HTTPException(
                400,
                f"Cannot approve: workflow is in '{current_stage}' stage, expected '{expected_stage}'",
            )
        return handle
    except HTTPException:
        raise
    except Exception:
        doc = db.get_document(workflow_id)
        if doc:
            raise HTTPException(
                400,
                f"Cannot approve: workflow is in '{doc.get('stage')}' stage "
                "(completed/failed workflows cannot be approved)",
            )
        raise HTTPException(404, f"Workflow not found: {workflow_id}")


_APPROVAL_SIGNALS = {
    "ocr": DocumentPipelineWorkflow.approve_ocr,
    "translation": DocumentPipelineWorkflow.approve_translation,
    "chunks": DocumentPipelineWorkflow.approve_chunks,
    "ingestion": DocumentPipelineWorkflow.approve_ingestion,
}


async def signal_workflow_approval(
    workflow_id: str, *, expected_stage: str, approval: str
) -> None:
    """Validate and send a document approval signal by domain name."""
    try:
        signal_method = _APPROVAL_SIGNALS[approval]
    except KeyError as exc:
        raise ValueError(f"Unknown workflow approval: {approval}") from exc
    handle = await validate_approval_stage(workflow_id, expected_stage)
    await handle.signal(signal_method)


async def execute_bulk_approval_action(
    request: BulkWorkflowActionRequest,
    action: str,
    expected_stage: str,
    approval: str,
    user: AuthUser,
) -> BulkWorkflowActionResponse:
    """Validate and signal a batch of approval actions independently."""
    results: list[BulkWorkflowActionResult] = []
    for workflow_id in request.workflow_ids:
        doc = access.document_for_user_or_none(
            workflow_id, user, permission=Permission.REVIEW
        )
        if not doc:
            results.append(
                BulkWorkflowActionResult(
                    workflow_id=workflow_id,
                    ok=False,
                    action=action,
                    message="document_not_found",
                )
            )
            continue
        current_stage = doc.get("stage")
        if current_stage != expected_stage:
            results.append(
                BulkWorkflowActionResult(
                    workflow_id=workflow_id,
                    ok=False,
                    action=action,
                    message=f"invalid_stage:{current_stage}",
                )
            )
            continue
        if request.dry_run:
            results.append(
                BulkWorkflowActionResult(
                    workflow_id=workflow_id,
                    ok=True,
                    action=action,
                    message="would_execute",
                )
            )
            continue
        try:
            await signal_workflow_approval(
                workflow_id, expected_stage=expected_stage, approval=approval
            )
            results.append(
                BulkWorkflowActionResult(
                    workflow_id=workflow_id,
                    ok=True,
                    action=action,
                    message="queued",
                )
            )
        except Exception as exc:
            results.append(
                BulkWorkflowActionResult(
                    workflow_id=workflow_id,
                    ok=False,
                    action=action,
                    message=str(exc),
                )
            )

    return BulkWorkflowActionResponse(
        action=action,
        dry_run=request.dry_run,
        requested=len(request.workflow_ids),
        succeeded=sum(1 for result in results if result.ok),
        failed=sum(1 for result in results if not result.ok),
        results=results,
    )
