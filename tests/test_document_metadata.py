"""Document metadata edit (issue #44) — display_name only."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import pipeline.db as db_mod
from pipeline.auth.jwt import claims_to_user
from pipeline.models import DocumentMetadataUpdate
from pipeline.routers import documents


def _run(coro):
    return asyncio.run(coro)


def _reviewer(instance: str = "tenant-a"):
    return claims_to_user({"sub": "rev", "tenant_roles": {instance: ["content_curator"]}})


def _viewer(instance: str = "tenant-a"):
    return claims_to_user({"sub": "view", "tenant_roles": {instance: ["viewer"]}})


@pytest.fixture
def meta_doc(db_connection, monkeypatch):
    db_mod.create_tenant_row("tenant-a", display_name="Tenant A")
    db_mod.create_tenant_row("tenant-b", display_name="Tenant B")
    db_mod.upsert_document(
        document_id="doc-meta-1",
        workflow_id="wf-meta-1",
        filename="policy.pdf",
        filepath="/tmp/policy.pdf",
        stage="completed",
        instance="tenant-a",
        display_name=None,
    )
    db_mod.upsert_document(
        document_id="doc-meta-b",
        workflow_id="wf-meta-b",
        filename="other.pdf",
        filepath="/tmp/other.pdf",
        stage="completed",
        instance="tenant-b",
    )
    return "wf-meta-1"


def test_set_display_name_roundtrip(meta_doc):
    summary = _run(
        documents.update_document_metadata(
            meta_doc,
            DocumentMetadataUpdate(display_name="  Cattle Insurance Guide  "),
            _reviewer(),
        )
    )
    assert summary.display_name == "Cattle Insurance Guide"
    assert db_mod.get_document(meta_doc)["display_name"] == "Cattle Insurance Guide"

    cleared = _run(
        documents.update_document_metadata(
            meta_doc,
            DocumentMetadataUpdate(display_name=""),
            _reviewer(),
        )
    )
    assert cleared.display_name is None
    assert db_mod.get_document(meta_doc)["display_name"] is None


def test_metadata_requires_display_name_field(meta_doc):
    with pytest.raises(HTTPException) as exc:
        _run(documents.update_document_metadata(meta_doc, DocumentMetadataUpdate(), _reviewer()))
    assert exc.value.status_code == 400


def test_metadata_blocks_disabled_doc(meta_doc):
    db_mod.set_document_disabled(meta_doc, True)
    with pytest.raises(HTTPException) as exc:
        _run(
            documents.update_document_metadata(
                meta_doc,
                DocumentMetadataUpdate(display_name="Nope"),
                _reviewer(),
            )
        )
    assert exc.value.status_code == 400


def test_metadata_viewer_forbidden(meta_doc):
    """A member of the doc's tenant lacking REVIEW (viewer) is 403, not 404 —
    the doc exists and is reachable, but the role can't mutate it."""
    with pytest.raises(HTTPException) as exc:
        _run(
            documents.update_document_metadata(
                meta_doc,
                DocumentMetadataUpdate(display_name="Nope"),
                _viewer("tenant-a"),
            )
        )
    assert exc.value.status_code == 403


def test_metadata_cross_tenant_hidden(meta_doc):
    with pytest.raises(HTTPException) as exc:
        _run(
            documents.update_document_metadata(
                "wf-meta-b",
                DocumentMetadataUpdate(display_name="Leak"),
                _reviewer("tenant-a"),
            )
        )
    assert exc.value.status_code == 404


def test_metadata_audited(meta_doc):
    _run(
        documents.update_document_metadata(
            meta_doc,
            DocumentMetadataUpdate(display_name="Audited Name"),
            _reviewer(),
        )
    )
    logs = db_mod.get_audit_logs(meta_doc, action_type="set_metadata")
    assert logs
    assert logs[0]["field_name"] == "display_name"
