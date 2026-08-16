"""
Temporal worker for the OCR pipeline.
"""

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from .client import TASK_QUEUE, host
from .registry import ACTIVITIES, WORKFLOWS
from .. import db

# Configure verbose logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)

# Set Temporal SDK logging to INFO
logging.getLogger("temporalio").setLevel(logging.INFO)

async def main():
    """Start the worker."""
    translation_provider = os.environ.get("TRANSLATION_PROVIDER", "gemma_vllm").strip().lower()
    if translation_provider in {"gemma_vllm", "gemma4", "gemma"}:
        if not os.environ.get("TRANSLATION_VLLM_BASE_URL", "").strip():
            print("Error: TRANSLATION_VLLM_BASE_URL not set")
            return

    # Initialize SQLite database
    print("Initializing SQLite database...")
    db.init_db()

    temporal_host = host()
    max_concurrent_activities = int(os.environ.get("TEMPORAL_MAX_CONCURRENT_ACTIVITIES", "4"))
    print(f"Connecting to Temporal at {temporal_host}")
    print(f"Worker activity concurrency: {max_concurrent_activities}")

    client = await Client.connect(temporal_host)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        max_concurrent_activities=max_concurrent_activities,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
    )

    print(f"Worker started on queue: {TASK_QUEUE}")
    print("Waiting for workflows...")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
