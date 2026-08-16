"""Temporal workflow execution, reporting, reconciliation, and approval helpers."""

import asyncio
import hashlib
import json
import logging
import time
from typing import Optional

from fastapi import HTTPException

from .. import clients, db
from ..auth.models import AuthUser
from ..auth.permissions import Permission
from ..auth.tenancy import default_instance, normalize_instance
from ..models import (
    BulkWorkflowActionRequest,
    BulkWorkflowActionResponse,
    BulkWorkflowActionResult,
)
from . import access


TASK_QUEUE = clients.TASK_QUEUE


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


async def start_pipeline_workflow(run, *, args: list, id: str, instance: str):
    """Start a workflow with tenant memo and best-effort search attribute."""
    global _instance_search_attr_supported
    inst = normalize_instance(instance)
    memo = {"instance": inst}

    if _instance_search_attr_supported is not False:
        try:
            handle = await (await clients.get_temporal_client()).start_workflow(
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

    return await (await clients.get_temporal_client()).start_workflow(
        run,
        args=args,
        id=id,
        task_queue=TASK_QUEUE,
        memo=memo,
    )


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

    temporal_client = await clients.get_temporal_client_or_none()
    runtime = {
        "workflow_id": workflow_id,
        "sqlite_stage": doc.get("stage"),
        "sqlite_error_message": doc.get("error_message"),
        "temporal_connected": temporal_client is not None,
        "job": current_job,
        "chunking_progress": chunking_progress,
        "temporal": None,
    }
    if temporal_client is None:
        return runtime

    try:
        handle = temporal_client.get_workflow_handle(runtime_workflow_id)
        description = await handle.describe()
        temporal_state = None
        query_error = None
        try:
            temporal_state = await handle.query("get_state")
        except Exception as exc:
            query_error = str(exc)
        runtime["temporal"] = {
            "workflow_id": runtime_workflow_id,
            "run_id": description.run_id,
            "status": description.status.name,
            "close_time": description.close_time.isoformat() if description.close_time else None,
            "execution_time": (
                description.execution_time.isoformat() if description.execution_time else None
            ),
            "state": temporal_state,
            "query_error": query_error,
        }
    except Exception as exc:
        runtime["temporal"] = {
            "workflow_id": workflow_id,
            "status": "UNAVAILABLE",
            "error": str(exc),
        }
    return runtime


async def reconcile_single_document(doc: dict) -> dict:
    """Reconcile one materialized document against its Temporal execution."""
    workflow_id = doc.get("workflow_id")
    current_stage = doc.get("stage")
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
    try:
        handle = (await clients.get_temporal_client()).get_workflow_handle(runtime_workflow_id)
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
        db.update_document_stage(workflow_id, "failed", error_message="Workflow query timed out")
        return {
            "workflow_id": workflow_id,
            "action": "marked_failed",
            "from": current_stage,
            "reason": "query_timeout",
        }
    except Exception as exc:
        error_message = str(exc)
        if "not found" in error_message.lower() or "workflow task" in error_message.lower():
            db.update_document_stage(
                workflow_id, "failed", error_message="Workflow terminated or lost"
            )
            db.log_audit(
                workflow_id=workflow_id,
                document_id=doc.get("document_id", ""),
                action_type="reconcile_failed",
                metadata={"from_stage": current_stage, "reason": "workflow_not_found"},
            )
            return {
                "workflow_id": workflow_id,
                "action": "marked_failed",
                "from": current_stage,
                "reason": "workflow_not_found",
            }
        return {
            "workflow_id": workflow_id,
            "action": "error",
            "from": current_stage,
            "reason": error_message,
        }


async def validate_approval_stage(workflow_id: str, expected_stage: str):
    """Validate that a workflow is in the expected stage before approval."""
    try:
        handle = (await clients.get_temporal_client()).get_workflow_handle(workflow_id)
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


async def execute_bulk_approval_action(
    request: BulkWorkflowActionRequest,
    action: str,
    expected_stage: str,
    signal_method,
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
            handle = await validate_approval_stage(workflow_id, expected_stage)
            await handle.signal(signal_method)
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
