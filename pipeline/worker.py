"""Compatibility launcher for the Temporal worker.

Deployments historically start ``python -m pipeline.worker``. Keep that stable
while the worker implementation lives at the explicit Temporal boundary.
"""

import asyncio

from .temporal.client import TASK_QUEUE
from .temporal.worker import main

__all__ = ["TASK_QUEUE", "main"]


if __name__ == "__main__":
    asyncio.run(main())
