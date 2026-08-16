"""Lazy Temporal client owned by the Temporal adapter."""

import asyncio
import logging
import os
from typing import Optional

from temporalio.client import Client


TASK_QUEUE = "ocr-pipeline"

_client: Optional[Client] = None
_lock: Optional[asyncio.Lock] = None


def host() -> str:
    return os.environ.get("TEMPORAL_HOST", "localhost:7233")


async def get_client_or_none() -> Optional[Client]:
    """Return Temporal for reporting callers, degrading to ``None`` on outage."""
    try:
        return await asyncio.wait_for(get_client(), timeout=5.0)
    except Exception as exc:  # noqa: BLE001 - reporting must tolerate an outage
        logging.warning("Temporal unavailable: %s", exc)
        return None


async def get_client() -> Client:
    """Return the shared client, connecting on first operational use.

    Failed connections are not cached, so a later call retries.
    """
    global _client, _lock

    if _client is not None:
        return _client

    if _lock is None:
        _lock = asyncio.Lock()

    async with _lock:
        if _client is not None:
            return _client
        temporal_host = host()
        logging.info("Connecting to Temporal at %s", temporal_host)
        try:
            _client = await Client.connect(temporal_host)
        except Exception as exc:  # noqa: BLE001 - re-raised with context below
            raise RuntimeError(
                f"Could not connect to Temporal at {temporal_host}: {exc}"
            ) from exc
        return _client


def reset() -> None:
    """Drop the cached client and lock for tests or reconfiguration."""
    global _client, _lock
    _client = None
    _lock = None
