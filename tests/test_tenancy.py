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
    assert user_can_access_instance(user, "amul")
    assert user_can_access_instance(user, "bv")


def test_user_instances_are_enforced():
    user = claims_to_user(
        {
            "sub": "u1",
            "realm_access": {"roles": ["content_curator"]},
            "instances": ["Amul", "bv"],
        }
    )
    assert allowed_instances(user) == {"amul", "bv"}
    assert user_can_access_instance(user, "amul")
    assert not user_can_access_instance(user, "mh")
    with pytest.raises(HTTPException) as exc:
        assert_instance_access(user, "mh")
    assert exc.value.status_code == 403


def test_document_access_hides_other_instances():
    user = claims_to_user(
        {
            "sub": "u1",
            "realm_access": {"roles": ["viewer"]},
            "instances": ["amul"],
        }
    )
    with pytest.raises(HTTPException) as exc:
        assert_document_instance_access(user, {"workflow_id": "wf", "instance": "bv"})
    assert exc.value.status_code == 404


def test_list_documents_filters_by_instance(db_connection):
    db = db_connection
    db.upsert_document(
        workflow_id="wf-amul",
        document_id="d1",
        filename="a.pdf",
        filepath="/tmp/a.pdf",
        stage="completed",
        instance="amul",
    )
    db.upsert_document(
        workflow_id="wf-bv",
        document_id="d2",
        filename="b.pdf",
        filepath="/tmp/b.pdf",
        stage="completed",
        instance="bv",
    )

    amul_only = db.list_documents(instances=["amul"])
    assert {d["workflow_id"] for d in amul_only} == {"wf-amul"}

    both = db.list_documents(instances=["amul", "bv"])
    assert {d["workflow_id"] for d in both} == {"wf-amul", "wf-bv"}

    none = db.list_documents(instances=[])
    assert none == []


def test_summary_counts_honor_instance_filter(db_connection):
    db = db_connection
    db.upsert_document(
        workflow_id="wf-amul-2",
        document_id="d3",
        filename="c.pdf",
        filepath="/tmp/c.pdf",
        stage="completed",
        instance="amul",
    )
    db.upsert_document(
        workflow_id="wf-bv-2",
        document_id="d4",
        filename="d.pdf",
        filepath="/tmp/d.pdf",
        stage="failed",
        instance="bv",
    )
    summary = db.get_document_summary_counts(instances=["amul"])
    assert summary["total_documents"] == 1
    assert summary["completed_documents"] == 1
    assert summary["failed_documents"] == 0
