"""Start advisory index / promote workflows: one Temporal workflow per webhook record."""

from __future__ import annotations

import logging
import os

from temporalio.exceptions import WorkflowAlreadyStartedError

from .workflows import AdvisoryIndexWorkflow, AdvisoryPromoteWorkflow

logger = logging.getLogger(__name__)

TASK_QUEUE = "ocr-pipeline"


async def _start_each(client, workflow_run, id_prefix: str, record_ids: list[str]) -> int:
    started = 0
    for record_id in record_ids:
        try:
            await client.start_workflow(
                workflow_run,
                args=[record_id],
                id=f"{id_prefix}{record_id}",
                task_queue=TASK_QUEUE,
            )
            started += 1
        except WorkflowAlreadyStartedError:
            logger.info("advisory workflow %s%s already running; skipped", id_prefix, record_id)
        except Exception:
            logger.exception("could not start advisory workflow %s%s", id_prefix, record_id)
    return started


async def start_advisory_index(client, record_ids: list[str]) -> int:
    """Start ``AdvisoryIndexWorkflow`` (id ``advisory-<record id>``) for each id; returns how many started."""
    return await _start_each(client, AdvisoryIndexWorkflow.run, "advisory-", record_ids)


async def start_advisory_promote(client, record_ids: list[str]) -> int:
    """Start ``AdvisoryPromoteWorkflow`` (id ``advisory-promote-<record id>``) for each id."""
    return await _start_each(client, AdvisoryPromoteWorkflow.run, "advisory-promote-", record_ids)


def _backfill_enabled() -> bool:
    return os.environ.get("ADVISORY_BACKFILL_ON_START", "false").strip().lower() in {"1", "true", "yes", "on"}


async def backfill_on_start(client) -> int:
    """Worker startup hook: when ADVISORY_BACKFILL_ON_START is on (default off), queue every unindexed row."""
    if not _backfill_enabled():
        return 0
    from . import db

    record_ids = db.get_unindexed_webhook_record_ids()
    started = await start_advisory_index(client, record_ids)
    logger.info("advisory startup backfill: %s unindexed rows, %s workflows started", len(record_ids), started)
    return started
