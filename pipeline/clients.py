"""
Lazy accessors for the API's external clients (Temporal, MinIO).

Why this module exists: both clients used to be connected eagerly in the
FastAPI lifespan, which meant `pipeline.api` could not even be imported into a
TestClient without a live Temporal server and a live MinIO. Connecting on FIRST
USE instead keeps the failure mode identical for routes that genuinely need
those backends (they still raise loudly, at call time) while letting everything
else — health, docs, SQLite-only routes, the whole test suite — run without
them.
"""

import asyncio
import logging
import os
from typing import Optional

from minio import Minio
from temporalio.client import Client

TASK_QUEUE = "ocr-pipeline"
MINIO_BUCKET = os.environ.get("MINIO_BUCKET", "documents")

_temporal_client: Optional[Client] = None
_temporal_lock: Optional[asyncio.Lock] = None

_minio_client: Optional[Minio] = None


def temporal_host() -> str:
    return os.environ.get("TEMPORAL_HOST", "localhost:7233")


def minio_bucket() -> str:
    """Bucket name, re-read from the environment so tests can override it."""
    return os.environ.get("MINIO_BUCKET", MINIO_BUCKET)


async def get_temporal_client() -> Client:
    """Return the shared Temporal client, connecting on first use.

    A connection failure is raised to the caller (as ``RuntimeError`` carrying
    the underlying cause) and nothing is cached, so the next call retries.
    """
    global _temporal_client, _temporal_lock

    if _temporal_client is not None:
        return _temporal_client

    if _temporal_lock is None:
        _temporal_lock = asyncio.Lock()

    async with _temporal_lock:
        if _temporal_client is not None:
            return _temporal_client
        host = temporal_host()
        logging.info("Connecting to Temporal at %s", host)
        try:
            _temporal_client = await Client.connect(host)
        except Exception as exc:  # noqa: BLE001 - re-raised with context below
            raise RuntimeError(
                f"Could not connect to Temporal at {host}: {exc}"
            ) from exc
        return _temporal_client


def get_minio_client() -> Minio:
    """Return the shared MinIO client, constructing it on first use.

    Mirrors what the lifespan used to do: credentials are required, and the
    configured bucket is created if missing. Both failures surface here, at
    call time, instead of at startup.
    """
    global _minio_client

    if _minio_client is not None:
        return _minio_client

    access_key = os.environ.get("MINIO_ACCESS_KEY")
    secret_key = os.environ.get("MINIO_SECRET_KEY")
    if not access_key or not secret_key:
        raise RuntimeError(
            "MINIO_ACCESS_KEY and MINIO_SECRET_KEY environment variables are required"
        )

    endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    logging.info("Connecting to MinIO at %s", endpoint)
    client = Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=False,
    )

    bucket = minio_bucket()
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            logging.info("Created MinIO bucket: %s", bucket)
    except Exception as exc:  # noqa: BLE001 - re-raised with context below
        raise RuntimeError(
            f"Could not reach MinIO at {endpoint}: {exc}"
        ) from exc

    _minio_client = client
    return _minio_client


def reset() -> None:
    """Drop cached clients (tests / reconfiguration)."""
    global _temporal_client, _minio_client, _temporal_lock
    _temporal_client = None
    _minio_client = None
    _temporal_lock = None
