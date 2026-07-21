"""Tests for auth follow-ups: enablement matrix + Keycloak admin config."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.auth.keycloak_admin import (
    ALLOWED_REALM_ROLES,
    KeycloakAdminClient,
    KeycloakAdminConfig,
    KeycloakAdminError,
    load_keycloak_admin_config,
)
from pipeline.auth.permissions import Permission


def test_load_keycloak_admin_config_from_issuer(monkeypatch):
    monkeypatch.setenv(
        "KEYCLOAK_ISSUER", "http://localhost:8082/auth/realms/docs-pipeline"
    )
    monkeypatch.setenv("KEYCLOAK_ADMIN_PASSWORD", "secret")
    monkeypatch.delenv("KEYCLOAK_ADMIN_BASE_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_BASE_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    cfg = load_keycloak_admin_config()
    assert cfg is not None
    assert cfg.base_url == "http://localhost:8082/auth"
    assert cfg.realm == "docs-pipeline"


def test_load_keycloak_admin_config_missing_password(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER", "http://localhost:8082/auth/realms/docs-pipeline")
    monkeypatch.delenv("KEYCLOAK_ADMIN_PASSWORD", raising=False)
    assert load_keycloak_admin_config() is None


def test_document_enablement_roundtrip(db_connection):
    db = db_connection
    db.upsert_document(
        document_id="doc-1",
        workflow_id="wf-enable-1",
        filename="a.pdf",
        filepath="/tmp/a.pdf",
        stage="completed",
        instance="tenant-a",
    )
    updated = db.set_document_enablement(
        "wf-enable-1", enabled_dev=True, enabled_prod=False
    )
    assert updated is not None
    assert int(updated["enabled_dev"]) == 1
    assert int(updated["enabled_prod"]) == 0

    again = db.set_document_enablement("wf-enable-1", enabled_prod=True)
    assert int(again["enabled_dev"]) == 1
    assert int(again["enabled_prod"]) == 1


def test_enablement_summary_includes_flags(db_connection):
    """Document summaries expose enablement + action for the matrix UI."""
    from pipeline.api import _document_summary_from_row

    db_connection.upsert_document(
        document_id="doc-2",
        workflow_id="wf-enable-2",
        filename="b.pdf",
        filepath="/tmp/b.pdf",
        stage="completed",
        instance="tenant-a",
    )
    db_connection.set_document_enablement(
        "wf-enable-2", enabled_dev=False, enabled_prod=True
    )
    doc = db_connection.get_document("wf-enable-2")
    summary = _document_summary_from_row(doc)
    assert summary.enabled_dev is False
    assert summary.enabled_prod is True
    assert summary.is_disabled is False
    assert "set_enablement" in summary.available_actions


@pytest.mark.asyncio
async def test_enablement_audit_records_actor_identity(db_connection):
    from pipeline.api import set_document_enablement
    from pipeline.auth.models import local_bypass_user
    from pipeline.models import DocumentEnablementUpdate

    db_connection.upsert_document(
        document_id="doc-audit",
        workflow_id="wf-enable-audit",
        filename="audit.pdf",
        filepath="/tmp/audit.pdf",
        stage="completed",
        instance="tenant-a",
    )
    user = local_bypass_user()
    await set_document_enablement(
        "wf-enable-audit",
        DocumentEnablementUpdate(enabled_dev=False),
        user,
    )
    logs = db_connection.get_audit_logs(
        "wf-enable-audit", action_type="set_enablement"
    )
    assert len(logs) == 1
    import json

    metadata = json.loads(logs[0]["metadata"])
    assert metadata["actor"] == user.user_id
    assert metadata["actor_username"] == user.username
    assert metadata["actor_email"] == user.email


def test_admin_users_requires_keycloak_config(monkeypatch):
    from fastapi import HTTPException
    from pipeline.api import _keycloak_admin_or_503

    monkeypatch.delenv("KEYCLOAK_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("KEYCLOAK_ISSUER", raising=False)
    monkeypatch.delenv("KEYCLOAK_ADMIN_BASE_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_BASE_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    with pytest.raises(HTTPException) as exc:
        _keycloak_admin_or_503()
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_admin_users_list_uses_keycloak_client():
    from pipeline import api as api_mod

    fake = MagicMock()
    fake.list_users.return_value = [
        {
            "id": "u1",
            "username": "docs-viewer",
            "instances": ["tenant-a"],
            "envs": ["dev"],
            "roles": [],
        }
    ]
    fake.get_user.return_value = {
        "id": "u1",
        "username": "docs-viewer",
        "instances": ["tenant-a"],
        "envs": ["dev"],
        "roles": ["viewer"],
        "enabled": True,
    }

    with patch.object(
        api_mod,
        "_keycloak_admin_or_503",
        return_value=(fake, KeycloakAdminError),
    ):
        payload = await api_mod.list_managed_users(
            user=MagicMock(), search=None, first=0, max_results=50
        )
    assert payload["count"] == 1
    assert payload["users"][0]["roles"] == ["viewer"]


def test_update_access_rejects_unknown_roles():
    client = KeycloakAdminClient(
        KeycloakAdminConfig(
            base_url="http://kc/auth",
            realm="docs-pipeline",
            admin_user="admin",
            admin_password="x",
        )
    )
    with pytest.raises(KeycloakAdminError) as exc:
        client.update_user_access("u1", roles=["not_a_real_role"])
    assert exc.value.status_code == 400


def test_allowed_realm_roles_match_permission_map():
    assert "master_admin" in ALLOWED_REALM_ROLES
    assert Permission.MANAGE_USERS.value == "manage_users"


def test_bootstrap_fixtures_cover_all_roles():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "keycloak_bootstrap_docs_pipeline.py"
    spec = importlib.util.spec_from_file_location("kc_bootstrap", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    roles = {f["role"] for f in mod.EXAMPLE_FIXTURES}
    assert roles == {"master_admin", "admin", "content_curator", "viewer"}
