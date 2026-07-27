"""Upload deduplication (#43).

Contract: the same file uploaded into the same tenant returns HTTP 200 with the
EXISTING document and ``duplicate=True`` and starts NO new pipeline. ``force=true``
bypasses the check and re-ingests. The same file in a DIFFERENT tenant is allowed
(dedup is per-tenant). A soft-deleted/disabled match is surfaced (is_disabled +
restore action) rather than silently reused.

Route handlers are invoked directly (the tenant-isolation suite's style): the app
lifespan needs a live Temporal connection, so the functions are called with
explicit Query/UploadFile args and the module clients mocked.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.datastructures import Headers, UploadFile

import pipeline.api as api
import pipeline.db as db_mod
from pipeline.auth.jwt import claims_to_user


def _run(coro):
    return asyncio.run(coro)


PDF_BYTES = b"%PDF-1.4 minimal dedup test body"
A = "tenant-a"
B = "tenant-b"


def _curator_in(instance: str):
    return claims_to_user({"sub": "cur", "tenant_roles": {instance: ["content_curator"]}})


def _upload_file(content: bytes = PDF_BYTES, filename: str = "doc.pdf") -> UploadFile:
    return UploadFile(
        BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": "application/pdf"}),
    )


@pytest.fixture
def wired(db_connection, monkeypatch):
    """api bound to the test db with mocked Temporal + MinIO clients."""
    monkeypatch.setattr(api, "db", db_mod)
    temporal = AsyncMock()
    handle = AsyncMock()
    handle.query = AsyncMock(return_value={"stage": "registered"})
    temporal.get_workflow_handle = MagicMock(return_value=handle)
    temporal.start_workflow = AsyncMock(return_value=handle)
    monkeypatch.setattr(api, "temporal_client", temporal)
    monkeypatch.setattr(api, "minio_client", MagicMock())
    # Direct handler calls shouldn't trip the IP rate limiter.
    monkeypatch.setattr(api.limiter, "enabled", False)
    # Reset the module's one-shot Temporal search-attribute feature flag.
    api._instance_search_attr_supported = None
    return temporal


def _upload(user, *, instance: str, force: bool = False, content: bytes = PDF_BYTES):
    return _run(
        api.upload_and_process(
            MagicMock(),  # request (rate limiter disabled in tests)
            user,
            file=_upload_file(content=content),
            auto_approve=True,
            instance=instance,
            force=force,
        )
    )


# --- db layer ----------------------------------------------------------------


def test_find_document_by_fingerprint_is_tenant_scoped(db_connection):
    db = db_connection
    db.upsert_document(
        workflow_id="wf-a",
        document_id="d-a",
        filename="a.pdf",
        filepath="/tmp/a.pdf",
        stage="completed",
        instance=A,
        source_file_fingerprint="fp-123",
    )
    found = db.find_document_by_fingerprint(A, "fp-123")
    assert found is not None and found["workflow_id"] == "wf-a"
    # Same fingerprint, different tenant -> not a duplicate.
    assert db.find_document_by_fingerprint(B, "fp-123") is None
    # Unknown fingerprint / empty input -> None.
    assert db.find_document_by_fingerprint(A, "nope") is None
    assert db.find_document_by_fingerprint(A, "") is None


# --- upload dedup path -------------------------------------------------------


def test_duplicate_same_tenant_returns_existing_and_starts_no_workflow(wired):
    temporal = wired
    user = _curator_in(A)

    first = _upload(user, instance=A)
    assert first.duplicate is False
    assert temporal.start_workflow.await_count == 1

    second = _upload(user, instance=A)
    assert second.duplicate is True
    assert second.workflow_id == first.workflow_id
    # No second pipeline was started.
    assert temporal.start_workflow.await_count == 1


def test_force_bypasses_dedup_and_reingests(wired):
    temporal = wired
    user = _curator_in(A)

    _upload(user, instance=A)
    assert temporal.start_workflow.await_count == 1

    forced = _upload(user, instance=A, force=True)
    assert forced.duplicate is False
    # A fresh run was started despite the identical file.
    assert temporal.start_workflow.await_count == 2


def test_same_file_different_tenant_is_allowed(wired):
    temporal = wired

    _upload(_curator_in(A), instance=A)
    assert temporal.start_workflow.await_count == 1

    # Identical bytes into tenant-b -> not a duplicate, a new run starts.
    other = _upload(_curator_in(B), instance=B)
    assert other.duplicate is False
    assert temporal.start_workflow.await_count == 2


def test_disabled_duplicate_surfaces_restore(wired):
    """A soft-deleted match is reported (is_disabled + restore action), not reused."""
    user = _curator_in(A)
    first = _upload(user, instance=A)
    db_mod.set_document_disabled(first.workflow_id, True)

    dup = _upload(user, instance=A)
    assert dup.duplicate is True
    assert dup.is_disabled is True
    assert "restore_document" in dup.available_actions
