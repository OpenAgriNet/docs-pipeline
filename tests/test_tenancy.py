"""Unit tests for multi-instance (tenant) scoping."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from pipeline.auth.jwt import claims_to_user
from pipeline.auth.models import local_bypass_user
from pipeline.auth.tenancy import (
    assert_document_instance_access,
    assert_instance_access,
    allowed_instances,
    user_can_access_instance,
)


def test_bypass_user_is_unrestricted():
    user = local_bypass_user()
    assert allowed_instances(user) is None
    assert user_can_access_instance(user, "tenant-a")
    assert user_can_access_instance(user, "tenant-b")


def test_user_instances_are_enforced():
    user = claims_to_user(
        {
            "sub": "u1",
            "realm_access": {"roles": ["content_curator"]},
            "instances": ["Tenant-A", "tenant-b"],
        }
    )
    assert allowed_instances(user) == {"tenant-a", "tenant-b"}
    assert user_can_access_instance(user, "tenant-a")
    assert not user_can_access_instance(user, "mh")
    with pytest.raises(HTTPException) as exc:
        assert_instance_access(user, "mh")
    assert exc.value.status_code == 403


def test_document_access_hides_other_instances():
    user = claims_to_user(
        {
            "sub": "u1",
            "realm_access": {"roles": ["viewer"]},
            "instances": ["tenant-a"],
        }
    )
    with pytest.raises(HTTPException) as exc:
        assert_document_instance_access(user, {"workflow_id": "wf", "instance": "tenant-b"})
    assert exc.value.status_code == 404


def test_superadmin_token_is_instance_unrestricted_despite_scoped_claim():
    """Platform superadmin stays unrestricted even with a narrow instances claim."""
    for role in ("superadmin", "super_admin", "master_admin"):
        user = claims_to_user(
            {
                "sub": "admin-1",
                "realm_access": {"roles": [role]},
                "instances": ["tenant-a"],  # scoped claim must NOT limit superadmin
            }
        )
        assert user.is_superadmin is True
        assert user.is_admin is True  # alias
        assert user.is_instance_unrestricted() is True
        # Unrestricted -> allowed_instances is None (every instance visible).
        assert allowed_instances(user) is None
        assert user_can_access_instance(user, "tenant-b")
        doc = assert_document_instance_access(
            user, {"workflow_id": "wf", "instance": "tenant-b"}
        )
        assert doc["instance"] == "tenant-b"


def test_state_admin_is_scoped_to_instances_claim():
    """State-level admin is limited to claimed instances (not platform-wide)."""
    from pipeline.auth.permissions import Permission

    user = claims_to_user(
        {
            "sub": "state-1",
            "realm_access": {"roles": ["admin"]},
            "instances": ["tenant-a"],
        }
    )
    assert user.is_superadmin is False
    assert user.is_instance_unrestricted() is False
    assert allowed_instances(user) == {"tenant-a"}
    assert user_can_access_instance(user, "tenant-a")
    assert not user_can_access_instance(user, "tenant-b")
    assert Permission.SEARCH in user.permissions
    assert Permission.UPLOAD in user.permissions
    assert Permission.ADMIN not in user.permissions
    with pytest.raises(HTTPException) as exc:
        assert_document_instance_access(user, {"workflow_id": "wf", "instance": "tenant-b"})
    assert exc.value.status_code == 404


def test_content_curator_with_scoped_claim_cannot_cross_tenants():
    """Non-superadmin roles remain limited to their claimed instances."""
    user = claims_to_user(
        {
            "sub": "curator-1",
            "realm_access": {"roles": ["content_curator"]},
            "instances": ["tenant-a"],
        }
    )
    assert user.is_admin is False
    assert user.is_instance_unrestricted() is False
    assert allowed_instances(user) == {"tenant-a"}
    assert user_can_access_instance(user, "tenant-a")
    assert not user_can_access_instance(user, "tenant-b")
    with pytest.raises(HTTPException) as exc:
        assert_document_instance_access(user, {"workflow_id": "wf", "instance": "tenant-b"})
    assert exc.value.status_code == 404


def test_list_documents_filters_by_instance(db_connection):
    db = db_connection
    db.upsert_document(
        workflow_id="wf-tenant-a",
        document_id="d1",
        filename="a.pdf",
        filepath="/tmp/a.pdf",
        stage="completed",
        instance="tenant-a",
    )
    db.upsert_document(
        workflow_id="wf-tenant-b",
        document_id="d2",
        filename="b.pdf",
        filepath="/tmp/b.pdf",
        stage="completed",
        instance="tenant-b",
    )

    tenant_a_only = db.list_documents(instances=["tenant-a"])
    assert {d["workflow_id"] for d in tenant_a_only} == {"wf-tenant-a"}

    both = db.list_documents(instances=["tenant-a", "tenant-b"])
    assert {d["workflow_id"] for d in both} == {"wf-tenant-a", "wf-tenant-b"}

    none = db.list_documents(instances=[])
    assert none == []


def test_summary_counts_honor_instance_filter(db_connection):
    db = db_connection
    db.upsert_document(
        workflow_id="wf-tenant-a-2",
        document_id="d3",
        filename="c.pdf",
        filepath="/tmp/c.pdf",
        stage="completed",
        instance="tenant-a",
    )
    db.upsert_document(
        workflow_id="wf-tenant-b-2",
        document_id="d4",
        filename="d.pdf",
        filepath="/tmp/d.pdf",
        stage="failed",
        instance="tenant-b",
    )
    summary = db.get_document_summary_counts(instances=["tenant-a"])
    assert summary["total_documents"] == 1
    assert summary["completed_documents"] == 1
    assert summary["failed_documents"] == 0


def test_upsert_does_not_reassign_instance(db_connection):
    db = db_connection
    db.upsert_document(
        workflow_id="wf-owned",
        document_id="d1",
        filename="a.pdf",
        filepath="/tmp/a.pdf",
        stage="registered",
        instance="tenant-a",
    )
    db.upsert_document(
        workflow_id="wf-owned",
        document_id="d1",
        filename="a.pdf",
        filepath="/tmp/a.pdf",
        stage="ocr_review",
        instance="tenant-b",  # must be ignored on update
    )
    doc = db.get_document("wf-owned")
    assert doc["instance"] == "tenant-a"
    assert doc["stage"] == "ocr_review"


def test_api_helpers_hide_cross_tenant_mutations(db_connection, monkeypatch):
    """Mutation helpers must 404 (not 403) for other tenants."""
    import pipeline.api as api
    import pipeline.db as db_mod

    monkeypatch.setattr(api, "db", db_mod)
    db_mod.upsert_document(
        workflow_id="wf-tenant-b-doc",
        document_id="d-tenant-b",
        filename="b.pdf",
        filepath="/tmp/b.pdf",
        stage="ocr_review",
        instance="tenant-b",
    )
    user = claims_to_user(
        {
            "sub": "u1",
            "realm_access": {"roles": ["content_curator"]},
            "instances": ["tenant-a"],
        }
    )
    with pytest.raises(HTTPException) as exc:
        api._require_document_for_user("wf-tenant-b-doc", user)
    assert exc.value.status_code == 404
    assert api._document_for_user_or_none("wf-tenant-b-doc", user) is None
