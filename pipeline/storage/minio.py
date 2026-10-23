"""Lazy MinIO client and bucket configuration."""

import logging
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from minio import Minio
from minio.error import S3Error

from ..instances import normalize_instance


DEFAULT_BUCKET = "documents"
_client: Optional[Minio] = None


def bucket_name() -> str:
    """Return the configured bucket, re-reading the environment for tests."""
    return os.environ.get("MINIO_BUCKET", DEFAULT_BUCKET)


def get_client() -> Minio:
    """Return the shared MinIO client, constructing it on first use.

    First use verifies or creates the configured bucket. A failed first use
    caches nothing, so the next call (or Temporal retry) connects again.
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


def artifact_object_name(instance: str | None, workflow_id: str, artifact_type: str, filename: str) -> str:
    """Object key for a document artifact, prefixed by its tenant for isolation.

    Layout: ``<instance>/<workflow_id>/<artifact_type>/<filename>``. New writes
    always carry the tenant prefix (including the default tenant). Reads never
    reconstruct this key — they use the stored ``minio://`` URI on the artifact
    row — so pre-prefix objects remain readable.
    """
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "artifact")
    return f"{normalize_instance(instance)}/{workflow_id}/{artifact_type}/{safe_name}"


def upload_artifact(
    local_path: str,
    workflow_id: str,
    artifact_type: str,
    filename: str,
    instance: str | None = None,
) -> tuple[str, int, str]:
    """Upload a local file as a document artifact; return ``(uri, size, mime)``."""
    bucket = bucket_name()
    object_name = artifact_object_name(instance, workflow_id, artifact_type, filename)
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    get_client().fput_object(bucket, object_name, local_path, content_type=mime_type)
    return (f"minio://{bucket}/{object_name}", os.path.getsize(local_path), mime_type)


def download_to_temp(storage_uri: str) -> str:
    """Download a ``minio://`` object to a temp file and return its path."""
    parsed = parse_minio_uri(storage_uri)
    if parsed is None:
        raise ValueError(f"Not a minio:// object URI: {storage_uri!r}")
    bucket, object_name = parsed
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(object_name).suffix) as temp_file:
        temp_path = temp_file.name
    get_client().fget_object(bucket, object_name, temp_path)
    return temp_path


def delete_object(bucket: str, object_name: str) -> None:
    """Remove one object. Missing keys are treated as already gone."""
    try:
        get_client().remove_object(bucket, object_name)
    except S3Error as exc:
        if getattr(exc, "code", None) in {"NoSuchKey", "NoSuchObject"}:
            return
        raise
