"""Translate Temporal SDK workflow failures into plain diagnostic data."""

import traceback

from temporalio.client import WorkflowFailureError


async def get_failure_details(handle) -> dict | None:
    """Await a workflow result and return structured failure details.

    Non-Temporal exceptions intentionally propagate so the application service
    can preserve its existing "could not retrieve" fallback.
    """
    try:
        await handle.result()
        return None
    except WorkflowFailureError as error:
        details = {
            "error_message": str(error),
            "error_type": type(error).__name__,
            "stack_trace": None,
        }
        cause = getattr(error, "cause", None)
        if cause:
            details["error_message"] = str(cause)
            details["error_type"] = type(cause).__name__
            cause_traceback = getattr(cause, "__traceback__", None)
            workflow_traceback = getattr(error, "__traceback__", None)
            if cause_traceback:
                details["stack_trace"] = "".join(traceback.format_tb(cause_traceback))
            elif workflow_traceback:
                details["stack_trace"] = "".join(
                    traceback.format_tb(workflow_traceback)
                )

        failure = getattr(error, "failure", None)
        if failure:
            if getattr(failure, "message", None):
                details["error_message"] = failure.message
            if getattr(failure, "stack_trace", None):
                details["stack_trace"] = failure.stack_trace
        return details
