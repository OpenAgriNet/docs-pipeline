"""Artifact garbage collection for disabled documents.

MinIO blobs are retained on soft-delete so restore/reingest still has sources.
Operators purge them explicitly (API ``apply=true`` or ``scripts/purge_document_artifacts.py --apply``).
"""

from __future__ import annotations

from .. import db
from ..storage import minio as minio_storage


class ArtifactPurgeError(ValueError):
    """Document is not eligible for artifact purge."""


def _artifact_summary(row: dict, **extra) -> dict:
    payload = {
        "id": row.get("id"),
        "artifact_type": row.get("artifact_type"),
        "storage_uri": row.get("storage_uri"),
        "filename": row.get("filename"),
    }
    payload.update(extra)
    return payload


def purge_document_artifacts(
    workflow_id: str,
    *,
    apply: bool = False,
    require_disabled: bool = True,
) -> dict:
    """Plan or apply MinIO deletes for one document's ``document_artifacts`` rows.

    Safe by default (``apply=False``): reports what would be purged vs retained.
    Already-purged rows and non-MinIO URIs are never deleted. SQLite metadata
    rows stay; successful deletes stamp ``purged_at``.
    """
    doc = db.get_document(workflow_id)
    if doc is None:
        raise ArtifactPurgeError("Document not found")
    if require_disabled and not doc.get("is_disabled"):
        raise ArtifactPurgeError(
            "Disable the document first; artifact purge is only for soft-deleted docs"
        )

    would_purge: list[dict] = []
    already_purged: list[dict] = []
    retained: list[dict] = []
    purged: list[dict] = []
    errors: list[dict] = []

    for row in db.list_document_artifacts(workflow_id):
        if row.get("purged_at"):
            already_purged.append(_artifact_summary(row, purged_at=row.get("purged_at")))
            continue
        parsed = minio_storage.parse_minio_uri(row.get("storage_uri"))
        if parsed is None:
            retained.append(_artifact_summary(row, reason="not_minio"))
            continue
        bucket, object_name = parsed
        item = _artifact_summary(row, bucket=bucket, object_name=object_name)
        if not apply:
            would_purge.append(item)
            continue
        try:
            minio_storage.delete_object(bucket, object_name)
            db.mark_artifact_purged(int(row["id"]))
            purged.append(item)
        except Exception as exc:  # noqa: BLE001 - report per-object, keep going
            errors.append({**item, "error": str(exc)})

    return {
        "workflow_id": workflow_id,
        "apply": bool(apply),
        "would_purge": would_purge,
        "purged": purged,
        "already_purged": already_purged,
        "retained": retained,
        "errors": errors,
        "would_purge_count": len(would_purge),
        "purged_count": len(purged),
        "already_purged_count": len(already_purged),
        "retained_count": len(retained),
        "error_count": len(errors),
    }
