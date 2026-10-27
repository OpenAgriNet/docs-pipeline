"""Multi-instance (tenant) scoping of callers, documents, runs and queues."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from pipeline.auth.jwt import claims_to_user
from pipeline.auth.models import local_bypass_user
from pipeline.auth.permissions import Permission
from pipeline.auth.tenancy import (
    allowed_instances,
    assert_document_instance_access,
    assert_instance_access,
    permissions_for,
    user_can_access_instance,
)
from pipeline.services import access
from tests.helpers import http_status, master_admin, member, seed_document

#: Curator-in-A / viewer-in-B.
SPLIT = {"tenant-a": ["content_curator"], "tenant-b": ["viewer"]}


def _flat(role, *instances):
    """A legacy flat-claim caller: one realm role, applied across ``instances``."""
    return claims_to_user({"sub": "u1", "realm_access": {"roles": [role]}, "instances": list(instances)})


def _seed_ab(db, stage="completed"):
    seed_document(db, "wf-a", document_id="da", stage=stage, instance="tenant-a")
    seed_document(db, "wf-b", document_id="db", stage=stage, instance="tenant-b")


def test_bypass_user_is_unrestricted():
    user = local_bypass_user()
    assert allowed_instances(user) is None
    assert user_can_access_instance(user, "tenant-a")
    assert user_can_access_instance(user, "tenant-b")


def test_user_instances_are_enforced():
    user = _flat("content_curator", "Tenant-A", "tenant-b")
    assert allowed_instances(user) == {"tenant-a", "tenant-b"}
    assert user_can_access_instance(user, "tenant-a")
    assert not user_can_access_instance(user, "mh")
    with pytest.raises(HTTPException) as exc:
        assert_instance_access(user, "mh")
    # 404, not 403, and the id is never echoed: out of reach reads exactly like non-existent.
    assert exc.value.status_code == 404
    assert "mh" not in exc.value.detail


def test_document_access_hides_other_instances():
    doc_b = {"workflow_id": "wf", "instance": "tenant-b"}
    assert http_status(lambda: assert_document_instance_access(_flat("viewer", "tenant-a"), doc_b)) == 404


def test_master_admin_is_control_plane_only_and_tenant_admin_is_scoped():
    """The realm ``master_admin`` manages the registry but its data scope is its tenant
    membership (none here); a per-tenant ``admin`` is full-permission inside its tenant only."""
    root = master_admin()
    assert allowed_instances(root) == set()
    assert user_can_access_instance(root, "tenant-b") is False
    doc_b = {"workflow_id": "wf", "instance": "tenant-b"}
    assert http_status(lambda: assert_document_instance_access(root, doc_b)) == 404

    tadmin = member("tenant-a", "admin")
    assert tadmin.is_instance_unrestricted() is False
    assert allowed_instances(tadmin) == {"tenant-a"}
    assert user_can_access_instance(tadmin, "tenant-a") is True
    assert user_can_access_instance(tadmin, "tenant-b") is False
    own = assert_document_instance_access(tadmin, {"workflow_id": "wf", "instance": "tenant-a"})
    assert own["instance"] == "tenant-a"
    with pytest.raises(HTTPException):
        assert_document_instance_access(tadmin, {"workflow_id": "wf2", "instance": "tenant-b"})


def test_content_curator_with_scoped_claim_cannot_cross_tenants():
    user = _flat("content_curator", "tenant-a")
    assert user.is_admin is False
    assert user.is_instance_unrestricted() is False
    assert allowed_instances(user) == {"tenant-a"}
    assert user_can_access_instance(user, "tenant-a")
    assert not user_can_access_instance(user, "tenant-b")
    doc_b = {"workflow_id": "wf", "instance": "tenant-b"}
    assert http_status(lambda: assert_document_instance_access(user, doc_b)) == 404


def test_list_documents_filters_by_instance(db_connection):
    _seed_ab(db_connection)
    assert {d["workflow_id"] for d in db_connection.list_documents(instances=["tenant-a"])} == {"wf-a"}
    both = db_connection.list_documents(instances=["tenant-a", "tenant-b"])
    assert {d["workflow_id"] for d in both} == {"wf-a", "wf-b"}
    assert db_connection.list_documents(instances=[]) == []


def test_summary_counts_honor_instance_filter(db_connection):
    seed_document(db_connection, "wf-a", stage="completed", instance="tenant-a")
    seed_document(db_connection, "wf-b", stage="failed", instance="tenant-b")
    summary = db_connection.get_document_summary_counts(instances=["tenant-a"])
    assert summary["total_documents"] == 1
    assert summary["completed_documents"] == 1
    assert summary["failed_documents"] == 0
    assert db_connection.get_document_summary_counts(instances=[])["total_documents"] == 0


def test_search_chunks_filters_by_instance(db_connection):
    seed_document(db_connection, "wf-a", instance="tenant-a", chunks=["cow feed"])
    seed_document(db_connection, "wf-b", instance="tenant-b", chunks=["cow feed"])

    def hits(instances):
        return {c["workflow_id"] for c in db_connection.search_chunks(query="cow", instances=instances)[0]}

    assert hits(["tenant-a"]) == {"wf-a"}
    assert hits([]) == set()


def test_upsert_does_not_reassign_instance(db_connection):
    seed_document(db_connection, "wf-owned", instance="tenant-a")
    seed_document(db_connection, "wf-owned", stage="ocr_review", instance="tenant-b")  # tenant-b must be ignored
    doc = db_connection.get_document("wf-owned")
    assert doc["instance"] == "tenant-a"
    assert doc["stage"] == "ocr_review"


def test_api_helpers_hide_cross_tenant_mutations(db_connection):
    """Mutation helpers must 404 (not 403) for other tenants."""
    seed_document(db_connection, "wf-b", stage="ocr_review", instance="tenant-b")
    user = _flat("content_curator", "tenant-a")
    assert http_status(lambda: access.require_document_for_user("wf-b", user)) == 404
    assert access.document_for_user_or_none("wf-b", user) is None


def test_permissions_for_helper_is_per_instance():
    user = member(SPLIT)
    assert Permission.REVIEW in permissions_for(user, "tenant-a")
    assert permissions_for(user, "tenant-b") == {Permission.SEARCH}
    assert permissions_for(user, "unknown") == set()


def test_wrong_role_in_valid_tenant_is_403_not_404(db_connection):
    """Curator-in-A / viewer-in-B may read B's document but not mutate it."""
    _seed_ab(db_connection, stage="ocr_review")
    user = member(SPLIT)
    assert access.require_document_for_user("wf-b", user)["instance"] == "tenant-b"
    assert http_status(lambda: access.require_document_for_user("wf-b", user, permission=Permission.REVIEW)) == 403
    assert access.document_for_user_or_none("wf-b", user, permission=Permission.REVIEW) is None
    assert access.require_document_for_user("wf-a", user, permission=Permission.REVIEW)["instance"] == "tenant-a"


def test_list_runs_filters_by_instance(db_connection):
    _seed_ab(db_connection)
    db_connection.create_document_job(workflow_id="wf-a", job_type="pipeline")
    db_connection.create_document_job(workflow_id="wf-b", job_type="pipeline")
    assert {r["workflow_id"] for r in db_connection.list_runs(instances=None)} == {"wf-a", "wf-b"}
    assert {r["workflow_id"] for r in db_connection.list_runs(instances=["tenant-a"])} == {"wf-a"}
    assert db_connection.list_runs(instances=[]) == []


def test_operations_queue_filters_by_instance(db_connection):
    _seed_ab(db_connection, stage="ocr_review")
    rows_a, total_a = db_connection.list_operations_queue(instances=["tenant-a"])
    assert {r["workflow_id"] for r in rows_a} == {"wf-a"}
    assert total_a == 1
    rows_all, total_all = db_connection.list_operations_queue(instances=None)
    assert {r["workflow_id"] for r in rows_all} == {"wf-a", "wf-b"}
    assert total_all == 2
    rows_none, total_none = db_connection.list_operations_queue(instances=[])
    assert rows_none == [] and total_none == 0
