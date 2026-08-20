"""Unit tests for multi-instance (tenant) scoping."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from pipeline.auth.jwt import claims_to_user
from pipeline.auth.models import local_bypass_user
from pipeline.auth.tenancy import (
    PORTAL_INSTANCE,
    assert_document_instance_access,
    assert_instance_access,
    allowed_instances,
    resolve_create_instance,
    user_can_access_instance,
)


def test_bypass_user_is_unrestricted():
    user = local_bypass_user()
    assert allowed_instances(user) is None
    assert user_can_access_instance(user, "tenant-a")
    assert user_can_access_instance(user, "tenant-b")


def test_resolve_create_instance_state_user_defaults_to_only_state():
    user = claims_to_user(
        {
            "sub": "u1",
            "groups": ["/states/MH/contributor"],
        }
    )
    assert resolve_create_instance(user, None) == "mh"
    assert resolve_create_instance(user, "MH") == "mh"
    with pytest.raises(HTTPException) as exc:
        resolve_create_instance(user, "up")
    assert exc.value.status_code == 403


def test_resolve_create_instance_superadmin_defaults_to_portal():
    user = claims_to_user(
        {
            "sub": "sa",
            "groups": ["/global/super-admin"],
        }
    )
    assert resolve_create_instance(user, None) == PORTAL_INSTANCE
    assert resolve_create_instance(user, "mh") == "mh"


def test_resolve_create_instance_multi_state_requires_choice():
    user = claims_to_user(
        {
            "sub": "u2",
            "groups": ["/states/MH/contributor", "/states/UP/view"],
        }
    )
    with pytest.raises(HTTPException) as exc:
        resolve_create_instance(user, None)
    assert exc.value.status_code == 400
    assert resolve_create_instance(user, "up") == "up"


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
    for role in ("superadmin", "super_admin", "super-admin"):
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


def test_list_runs_filters_by_instance(db_connection):
    """Runs must honor the same tenant scope as documents (role-based states)."""
    db = db_connection
    db.upsert_document(
        workflow_id="wf-run-a",
        document_id="d-run-a",
        filename="a.pdf",
        filepath="/tmp/a.pdf",
        stage="completed",
        instance="tenant-a",
    )
    db.upsert_document(
        workflow_id="wf-run-b",
        document_id="d-run-b",
        filename="b.pdf",
        filepath="/tmp/b.pdf",
        stage="completed",
        instance="tenant-b",
    )
    job_a = db.create_document_job("wf-run-a", "ocr", status="completed")
    job_b = db.create_document_job("wf-run-b", "ocr", status="running")

    tenant_a_only = db.list_runs(instances=["tenant-a"])
    assert {r["id"] for r in tenant_a_only} == {job_a}
    assert all(r.get("instance") == "tenant-a" for r in tenant_a_only)

    both = db.list_runs(instances=["tenant-a", "tenant-b"])
    assert {r["id"] for r in both} == {job_a, job_b}

    none = db.list_runs(instances=[])
    assert none == []

    unrestricted_rows = db.list_runs(instances=None)
    assert {r["id"] for r in unrestricted_rows} >= {job_a, job_b}

    # Status filter still works under instance scope.
    running_a = db.list_runs(status="running", instances=["tenant-a"])
    assert running_a == []
    running_b = db.list_runs(status="running", instances=["tenant-b"])
    assert {r["id"] for r in running_b} == {job_b}


def test_state_user_cannot_see_bv_portal_documents(db_connection):
    """State-scoped users must not see portal (BV) docs — only their state."""
    db = db_connection
    db.upsert_document(
        workflow_id="wf-bv",
        document_id="d-bv",
        filename="portal.pdf",
        filepath="/tmp/portal.pdf",
        stage="ocr_review",
        instance=PORTAL_INSTANCE,
    )
    db.upsert_document(
        workflow_id="wf-mh",
        document_id="d-mh",
        filename="mh.pdf",
        filepath="/tmp/mh.pdf",
        stage="ocr_review",
        instance="mh",
    )
    db.create_document_job("wf-bv", "ocr", status="running")
    db.create_document_job("wf-mh", "ocr", status="running")

    # MH contributor from Keycloak group path
    mh_user = claims_to_user(
        {
            "sub": "mh-user",
            "groups": ["/states/MH/contributor"],
            "realm_access": {"roles": ["contributor"]},
        }
    )
    assert mh_user.is_superadmin is False
    assert allowed_instances(mh_user) == {"mh"}
    assert not user_can_access_instance(mh_user, PORTAL_INSTANCE)
    assert not user_can_access_instance(mh_user, "bv")

    mh_docs = db.list_documents(instances=sorted(allowed_instances(mh_user) or []))
    assert {d["workflow_id"] for d in mh_docs} == {"wf-mh"}

    mh_runs = db.list_runs(instances=sorted(allowed_instances(mh_user) or []))
    assert all(r.get("instance") == "mh" for r in mh_runs)
    assert {r["workflow_id"] for r in mh_runs} == {"wf-mh"}

    mh_queue, mh_total = db.list_operations_queue(
        instances=sorted(allowed_instances(mh_user) or [])
    )
    assert mh_total == 1
    assert {r["workflow_id"] for r in mh_queue} == {"wf-mh"}

    with pytest.raises(HTTPException) as exc:
        assert_document_instance_access(
            mh_user, {"workflow_id": "wf-bv", "instance": PORTAL_INSTANCE}
        )
    assert exc.value.status_code == 404

    # Super-admin sees both
    sa = claims_to_user({"sub": "sa", "groups": ["/global/super-admin"]})
    assert allowed_instances(sa) is None
    assert user_can_access_instance(sa, PORTAL_INSTANCE)
    all_docs = db.list_documents(instances=None)
    assert {d["workflow_id"] for d in all_docs} >= {"wf-bv", "wf-mh"}


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
