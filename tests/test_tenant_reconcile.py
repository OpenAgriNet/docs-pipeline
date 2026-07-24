"""Tests for the tenant-registry reconcile / backfill (Phase 5.1).

The gap this covers: the ``tenants`` registry (surfaced by the superadmin
*Tenants* view via ``GET /tenants``) was only ever populated by ``POST /tenants``,
so tenants that exist de-facto — through ``documents.instance``, the
``tenant_indexes`` registry, or a Keycloak Organization — never got a row. The
reconcile backfills them; ``POST /tenants`` becomes idempotent (adopt); startup
self-heals.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock

import pipeline.api as api
import pipeline.db as db_mod
import pipeline.keycloak_admin as kc
from pipeline.auth.deps import require_platform_admin
from pipeline.auth.jwt import claims_to_user


def _run(coro):
    return asyncio.run(coro)


def _master_admin():
    return claims_to_user({"sub": "root", "realm_access": {"roles": ["master_admin"]}})


def _tenant_admin_in(instance: str):
    return claims_to_user({"sub": "tadmin", "tenant_roles": {instance: ["admin"]}})


def _patch_marqo(monkeypatch):
    monkeypatch.setattr(api, "db", db_mod)
    monkeypatch.setattr(api, "_create_marqo_index_with_schema", MagicMock(return_value={}))
    monkeypatch.setattr(api, "_marqo_client", lambda: MagicMock())


def _no_kc(monkeypatch):
    """Make every Keycloak org lookup behave as 'admin unconfigured'."""
    def _raise():
        raise kc.KeycloakAdminUnconfigured("no secret")

    monkeypatch.setattr(api.keycloak_admin, "list_organizations", _raise)


# =============================================================================
# db.list_known_instances
# =============================================================================


def test_list_known_instances_unions_documents_and_indexes(db_connection):
    db = db_connection
    # Fresh DB seeds the default tenant's default index -> 'default' is known.
    assert db.list_known_instances() == ["default"]

    # A document under 'acme' makes 'acme' known even with no registry row.
    db.upsert_document(
        workflow_id="wf-1", document_id="d-1", filename="a.pdf",
        filepath="/tmp/a.pdf", instance="acme",
    )
    # An index registered under 'tenant-a' (no documents) is also known.
    db.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)

    assert db.list_known_instances() == ["acme", "default", "tenant-a"]
    # None of these implied a tenants row yet.
    assert db.get_tenant("acme") is None
    assert db.get_tenant("tenant-a") is None


def test_list_known_instances_normalizes_and_dedupes(db_connection):
    db = db_connection
    db.upsert_document(
        workflow_id="wf-x", document_id="d-x", filename="x.pdf",
        filepath="/tmp/x.pdf", instance="  ACME ",
    )
    assert "acme" in db.list_known_instances()


# =============================================================================
# db.create_tenant_row — idempotent, non-clobbering
# =============================================================================


def test_create_tenant_row_is_idempotent_and_non_clobbering(db_connection):
    db = db_connection
    first = db.create_tenant_row("acme", display_name="Acme Dairy")
    assert first["id"] == "acme"
    assert first["display_name"] == "Acme Dairy"
    # A second call is a no-op and must NOT overwrite the existing display_name.
    again = db.create_tenant_row("acme", display_name="Something Else")
    assert again["display_name"] == "Acme Dairy"
    assert again["status"] == "active"


# =============================================================================
# reconcile_tenants
# =============================================================================


def test_reconcile_backfills_instance_with_docs_but_no_row(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    _no_kc(monkeypatch)
    # 'acme' has documents (and an index) but no tenants row.
    db_mod.upsert_document(
        workflow_id="wf-1", document_id="d-1", filename="a.pdf",
        filepath="/tmp/a.pdf", instance="acme",
    )
    db_mod.create_index_row("acme", "default", "acme-index", is_default=True)
    assert db_mod.get_tenant("acme") is None

    tenants = api.reconcile_tenants(include_keycloak=True)
    ids = {t["id"] for t in tenants}
    # Both the legacy default and the backfilled 'acme' are now registered.
    assert {"default", "acme"} <= ids
    assert db_mod.get_tenant("acme") is not None
    # display_name falls back to the instance id when there is no KC org name.
    assert db_mod.get_tenant("acme")["display_name"] == "acme"


def test_reconcile_is_idempotent_on_rerun(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    _no_kc(monkeypatch)
    db_mod.upsert_document(
        workflow_id="wf-1", document_id="d-1", filename="a.pdf",
        filepath="/tmp/a.pdf", instance="acme",
    )
    first = api.reconcile_tenants(include_keycloak=True)
    second = api.reconcile_tenants(include_keycloak=True)
    assert {t["id"] for t in first} == {t["id"] for t in second}
    # Exactly one 'acme' row, no duplicates.
    assert sum(1 for t in second if t["id"] == "acme") == 1


def test_reconcile_tolerant_when_keycloak_unconfigured(db_connection, monkeypatch):
    """When list_organizations raises, reconcile still backfills the local set."""
    _patch_marqo(monkeypatch)

    def _boom():
        raise kc.KeycloakAdminUnconfigured("no secret")

    monkeypatch.setattr(api.keycloak_admin, "list_organizations", _boom)
    db_mod.upsert_document(
        workflow_id="wf-1", document_id="d-1", filename="a.pdf",
        filepath="/tmp/a.pdf", instance="acme",
    )
    tenants = api.reconcile_tenants(include_keycloak=True)
    assert db_mod.get_tenant("acme") is not None
    assert "acme" in {t["id"] for t in tenants}


def test_reconcile_merges_keycloak_orgs(db_connection, monkeypatch):
    """Keycloak-only tenants (org exists, no local docs/index) are backfilled and
    the KC org name is used as the display name."""
    _patch_marqo(monkeypatch)
    monkeypatch.setattr(
        api.keycloak_admin,
        "list_organizations",
        lambda: [
            {"name": "Tenant A", "id": "org-1", "alias": "tenant-a", "instance": "tenant-a"},
            {"name": "tenant-b", "id": "org-2", "alias": "tenant-b", "instance": "tenant-b"},
        ],
    )
    tenants = api.reconcile_tenants(include_keycloak=True)
    ids = {t["id"] for t in tenants}
    assert {"tenant-a", "tenant-b"} <= ids
    # KC org name preferred as display name.
    assert db_mod.get_tenant("tenant-a")["display_name"] == "Tenant A"


# =============================================================================
# POST /tenants/reconcile — gating + shape
# =============================================================================


def test_reconcile_route_returns_count(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    _no_kc(monkeypatch)
    db_mod.upsert_document(
        workflow_id="wf-1", document_id="d-1", filename="a.pdf",
        filepath="/tmp/a.pdf", instance="acme",
    )
    out = _run(api.reconcile_tenants_route(_master_admin()))
    assert out["count"] == len(out["reconciled"])
    assert "acme" in {t["id"] for t in out["reconciled"]}


def test_reconcile_route_requires_platform_admin(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    # A per-tenant admin is NOT a platform admin -> 403 at the gate.
    with pytest.raises(HTTPException) as exc:
        _run(require_platform_admin(_tenant_admin_in("acme")))
    assert exc.value.status_code == 403


# =============================================================================
# POST /tenants idempotency — adopt
# =============================================================================


def test_create_tenant_adopts_instance_with_existing_index(db_connection, monkeypatch):
    """'acme' already has a default index (e.g. a legacy physical index). A
    superadmin 'Create tenant -> acme' must ADOPT it: no duplicate default index,
    adopted=True, and the existing physical index is returned."""
    _patch_marqo(monkeypatch)
    _no_kc(monkeypatch)
    db_mod.create_index_row("acme", "default", "acme-legacy-index", is_default=True)
    assert db_mod.get_tenant("acme") is None

    out = _run(api.create_tenant_route({"instance": "acme"}, _master_admin()))
    assert out["adopted"] is True
    assert out["tenant"]["id"] == "acme"
    assert out["default_index"]["marqo_index"] == "acme-legacy-index"
    # No new Marqo index was provisioned (adopted the existing default).
    api._create_marqo_index_with_schema.assert_not_called()
    # Exactly one index still registered.
    assert len(db_mod.list_indexes("acme")) == 1


def test_create_tenant_adopts_row_only_tenant_provisions_default(db_connection, monkeypatch):
    """A tenant that exists only as a registry row (no index) is adopted and gets
    its default index provisioned once."""
    _patch_marqo(monkeypatch)
    _no_kc(monkeypatch)
    db_mod.create_tenant_row("orphan")
    assert db_mod.get_default_index("orphan") is None

    out = _run(api.create_tenant_route({"instance": "orphan"}, _master_admin()))
    assert out["adopted"] is True
    assert out["default_index"]["marqo_index"] == "t-orphan-default"
    api._create_marqo_index_with_schema.assert_called_once()


def test_create_new_tenant_is_not_adopted(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    _no_kc(monkeypatch)
    out = _run(api.create_tenant_route({"instance": "brand-new"}, _master_admin()))
    assert out["adopted"] is False
    assert out["default_index"]["marqo_index"] == "t-brand-new-default"
