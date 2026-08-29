"""Lazy MinIO client and bucket configuration."""

import logging
import os
from typing import Optional

from minio import Minio
from minio.error import S3Error


DEFAULT_BUCKET = "documents"
_client: Optional[Minio] = None


def bucket_name() -> str:
    """Return the configured bucket, re-reading the environment for tests."""
    return os.environ.get("MINIO_BUCKET", DEFAULT_BUCKET)


def get_client() -> Minio:
    """Return the shared MinIO client, constructing it on first use.

    This is the API-side client: it verifies or creates the configured bucket.
    Temporal document tasks retain their uncached worker-side factory because
    merging the two behaviors would change retry and startup semantics.
    """
    global _client

    if _client is not None:
        return _client

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

    bucket = bucket_name()
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            logging.info("Created MinIO bucket: %s", bucket)
    except Exception as exc:  # noqa: BLE001 - re-raised with context below
        raise RuntimeError(f"Could not reach MinIO at {endpoint}: {exc}") from exc

    _client = client
    return _client


def reset() -> None:
    """Drop the cached client for tests or reconfiguration."""
    global _client
    _client = None


def parse_minio_uri(storage_uri: str | None) -> Optional[tuple[str, str]]:
    """Return ``(bucket, object_name)`` for a ``minio://bucket/key`` URI."""
    if not storage_uri or not storage_uri.startswith("minio://"):
        return None
    path = storage_uri[len("minio://") :]
    if "/" not in path:
        return None
    bucket, object_name = path.split("/", 1)
    if not bucket or not object_name:
        return None
    return bucket, object_name


def delete_object(bucket: str, object_name: str) -> None:
    """Remove one object. Missing keys are treated as already gone."""
    try:
        get_client().remove_object(bucket, object_name)
    except S3Error as exc:
        if getattr(exc, "code", None) in {"NoSuchKey", "NoSuchObject"}:
            return
        raise
