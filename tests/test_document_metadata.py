"""Document metadata edit (issue #44): display_name only."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import pipeline.db as db_mod
from pipeline.models import DocumentMetadataUpdate
from pipeline.routers import documents
from tests.helpers import member, seed_document

A = "tenant-a"
WF = "wf-meta-1"


@pytest.fixture
def meta_doc(db_connection):
    db_mod.create_tenant_row(A, display_name="Tenant A")
    db_mod.create_tenant_row("tenant-b", display_name="Tenant B")
    seed_document(db_connection, WF, filename="policy.pdf", stage="completed", instance=A, display_name=None)
    seed_document(db_connection, "wf-meta-b", filename="other.pdf", stage="completed", instance="tenant-b")
    return WF


def _update(workflow_id, display_name=None, role="content_curator"):
    body = DocumentMetadataUpdate() if display_name is None else DocumentMetadataUpdate(display_name=display_name)
    return documents.update_document_metadata(workflow_id, body, member(A, role))


def _status(coro) -> int:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)
    return exc.value.status_code


def test_set_display_name_roundtrip(meta_doc):
    assert asyncio.run(_update(meta_doc, "  Cattle Insurance Guide  ")).display_name == "Cattle Insurance Guide"
    assert db_mod.get_document(meta_doc)["display_name"] == "Cattle Insurance Guide"

    assert asyncio.run(_update(meta_doc, "")).display_name is None
    assert db_mod.get_document(meta_doc)["display_name"] is None


def test_metadata_requires_display_name_field(meta_doc):
    assert _status(_update(meta_doc)) == 400


def test_metadata_blocks_disabled_doc(meta_doc):
    db_mod.set_document_disabled(meta_doc, True)
    assert _status(_update(meta_doc, "Nope")) == 400


def test_metadata_viewer_forbidden(meta_doc):
    """A viewer in the doc's tenant is 403, not 404: the doc is reachable, the role can't mutate it."""
    assert _status(_update(meta_doc, "Nope", role="viewer")) == 403


def test_metadata_cross_tenant_hidden(meta_doc):
    assert _status(_update("wf-meta-b", "Leak")) == 404


def test_metadata_audited(meta_doc):
    asyncio.run(_update(meta_doc, "Audited Name"))
    logs = db_mod.get_audit_logs(meta_doc, action_type="set_metadata")
    assert logs
    assert logs[0]["field_name"] == "display_name"
