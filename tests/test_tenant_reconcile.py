"""Tenant-registry reconcile and backfill (Phase 5.1).

The ``tenants`` registry behind ``GET /tenants`` was only filled by ``POST /tenants``,
so tenants that exist de facto (``documents.instance``, the ``tenant_indexes``
registry, a Keycloak Organization) never got a row. Reconcile backfills them,
``POST /tenants`` adopts instead of 409-ing, and startup self-heals.
"""

from __future__ import annotations

import asyncio

import pytest

import pipeline.db as db_mod
import pipeline.keycloak_admin as kc
from pipeline.routers import tenants as tenant_routes
from pipeline.services import tenants as tenant_service
from tests.helpers import master_admin, seed_document


@pytest.fixture
def no_kc(monkeypatch):
    """Every Keycloak org lookup behaves as "admin unconfigured"."""
    def _raise():
        raise kc.KeycloakAdminUnconfigured("no secret")

    monkeypatch.setattr(kc, "list_organizations", _raise)


def _create_tenant(instance):
    return asyncio.run(tenant_routes.create_tenant_route({"instance": instance}, master_admin()))


def test_list_known_instances_unions_documents_and_indexes(db_connection):
    db = db_connection
    assert db.list_known_instances() == ["default"], "a fresh DB seeds the default tenant's default index"
    seed_document(db, "wf-1", instance="acme")
    db.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    assert db.list_known_instances() == ["acme", "default", "tenant-a"]
    assert db.get_tenant("acme") is None, "being known implies no tenants row yet"
    assert db.get_tenant("tenant-a") is None


def test_list_known_instances_normalizes_and_dedupes(db_connection):
    seed_document(db_connection, "wf-x", instance="  ACME ")
    assert "acme" in db_connection.list_known_instances()


def test_create_tenant_row_is_idempotent_and_non_clobbering(db_connection):
    first = db_connection.create_tenant_row("acme", display_name="Acme Dairy")
    assert first["id"] == "acme"
    assert first["display_name"] == "Acme Dairy"
    again = db_connection.create_tenant_row("acme", display_name="Something Else")
    assert again["display_name"] == "Acme Dairy"
    assert again["status"] == "active"


def test_reconcile_backfills_instance_with_docs_but_no_row(db_connection, no_kc):
    """Also the tolerance check: Keycloak raising "unconfigured" still backfills the local set."""
    seed_document(db_connection, "wf-1", instance="acme")
    db_mod.create_index_row("acme", "default", "acme-index", is_default=True)
    assert db_mod.get_tenant("acme") is None
    ids = {t["id"] for t in tenant_service.reconcile_tenants(include_keycloak=True)}
    assert {"default", "acme"} <= ids
    assert db_mod.get_tenant("acme") is not None
    assert db_mod.get_tenant("acme")["display_name"] == "acme", "no KC org name, so the instance id"


def test_reconcile_is_idempotent_on_rerun(db_connection, no_kc):
    seed_document(db_connection, "wf-1", instance="acme")
    first = tenant_service.reconcile_tenants(include_keycloak=True)
    second = tenant_service.reconcile_tenants(include_keycloak=True)
    assert {t["id"] for t in first} == {t["id"] for t in second}
    assert sum(1 for t in second if t["id"] == "acme") == 1


def test_reconcile_merges_keycloak_orgs(db_connection, monkeypatch):
    """Keycloak-only tenants are backfilled, named after their org."""
    monkeypatch.setattr(kc, "list_organizations", lambda: [
        {"name": "Tenant A", "id": "org-1", "alias": "tenant-a", "instance": "tenant-a"},
        {"name": "tenant-b", "id": "org-2", "alias": "tenant-b", "instance": "tenant-b"},
    ])
    assert {"tenant-a", "tenant-b"} <= {t["id"] for t in tenant_service.reconcile_tenants(include_keycloak=True)}
    assert db_mod.get_tenant("tenant-a")["display_name"] == "Tenant A"


def test_reconcile_route_returns_count(db_connection, no_kc):
    seed_document(db_connection, "wf-1", instance="acme")
    out = asyncio.run(tenant_routes.reconcile_tenants_route(master_admin()))
    assert out["count"] == len(out["reconciled"])
    assert "acme" in {t["id"] for t in out["reconciled"]}


def test_create_tenant_adopts_instance_with_existing_index(db_connection, fake_marqo, no_kc):
    """'acme' already has a default (e.g. legacy) index: create ADOPTS it, with no second default."""
    db_mod.create_index_row("acme", "default", "acme-legacy-index", is_default=True)
    assert db_mod.get_tenant("acme") is None
    out = _create_tenant("acme")
    assert out["adopted"] is True
    assert out["tenant"]["id"] == "acme"
    assert out["default_index"]["marqo_index"] == "acme-legacy-index"
    # An unbound fake would not hide a create: the real client would fail the route with a 502.
    assert fake_marqo.created_indexes == []
    assert len(db_mod.list_indexes("acme")) == 1


def test_create_tenant_adopts_row_only_tenant_provisions_default(db_connection, fake_marqo, no_kc):
    db_mod.create_tenant_row("orphan")
    assert db_mod.get_default_index("orphan") is None
    out = _create_tenant("orphan")
    assert out["adopted"] is True
    assert out["default_index"]["marqo_index"] == "t-orphan-default"
    assert fake_marqo.created_indexes == ["t-orphan-default"]


def test_create_new_tenant_is_not_adopted(db_connection, fake_marqo, no_kc):
    out = _create_tenant("brand-new")
    assert out["adopted"] is False
    assert out["default_index"]["marqo_index"] == "t-brand-new-default"
