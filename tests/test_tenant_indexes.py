"""Tests for the tenant index registry, resolver, and management API (Phases 4+5)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock

import pipeline.api as api
import pipeline.db as db_mod
from pipeline.auth.jwt import claims_to_user
from pipeline.auth.models import local_bypass_user


def _run(coro):
    return asyncio.run(coro)


# --- test principals ---------------------------------------------------------


def _admin():
    """Unrestricted platform admin (holds Permission.ADMIN everywhere)."""
    return claims_to_user({"sub": "admin-1", "realm_access": {"roles": ["admin"]}})


def _master_admin():
    return claims_to_user({"sub": "root", "realm_access": {"roles": ["master_admin"]}})


def _curator_in(instance: str):
    """Tenant-scoped curator: has `pipeline` (not `admin`) inside `instance`."""
    return claims_to_user({"sub": "cur", "tenant_roles": {instance: ["content_curator"]}})


def _viewer_in(instance: str):
    return claims_to_user({"sub": "view", "tenant_roles": {instance: ["viewer"]}})


# =============================================================================
# Registry resolve
# =============================================================================


def test_seed_maps_existing_index_to_default_tenant_default(db_connection):
    db = db_connection
    # After init the registry seeds the legacy physical index as the default
    # tenant's default -> identical to today's single-index behaviour.
    row = db.get_default_index("default")
    assert row is not None
    assert row["marqo_index"] == "documents-index"
    assert bool(row["is_default"]) is True
    assert db.resolve_marqo_index("default") == "documents-index"


def test_resolve_default_and_named(db_connection):
    db = db_connection
    db.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    db.create_index_row("tenant-a", "schemes", "t-tenant-a-schemes")
    # name=None -> the tenant default
    assert db.resolve_marqo_index("tenant-a") == "t-tenant-a-vet"
    # named lookups
    assert db.resolve_marqo_index("tenant-a", "vet") == "t-tenant-a-vet"
    assert db.resolve_marqo_index("tenant-a", "schemes") == "t-tenant-a-schemes"
    # unknown name / tenant -> None (caller applies legacy fallback)
    assert db.resolve_marqo_index("tenant-a", "nope") is None
    assert db.resolve_marqo_index("ghost") is None


def test_multi_index_per_tenant_resolve_to_distinct_physical(db_connection):
    db = db_connection
    db.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    db.create_index_row("tenant-a", "schemes", "t-tenant-a-schemes")
    physical = {r["marqo_index"] for r in db.list_indexes("tenant-a")}
    assert physical == {"t-tenant-a-vet", "t-tenant-a-schemes"}
    assert db.resolve_marqo_index("tenant-a", "vet") != db.resolve_marqo_index("tenant-a", "schemes")


def test_create_index_row_first_is_default_second_is_not(db_connection):
    db = db_connection
    first = db.create_index_row("tenant-b", "a", "t-tenant-b-a", is_default=True)
    second = db.create_index_row("tenant-b", "b", "t-tenant-b-b")
    assert bool(first["is_default"]) is True
    assert bool(second["is_default"]) is False
    # Re-declaring a new default flips the old one off (one default per tenant).
    db.create_index_row("tenant-b", "c", "t-tenant-b-c", is_default=True)
    defaults = [r["name"] for r in db.list_indexes("tenant-b") if r["is_default"]]
    assert defaults == ["c"]


def test_delete_index_row(db_connection):
    db = db_connection
    db.create_index_row("tenant-c", "x", "t-tenant-c-x")
    assert db.get_index("tenant-c", "x") is not None
    assert db.delete_index_row("tenant-c", "x") is True
    assert db.get_index("tenant-c", "x") is None
    assert db.delete_index_row("tenant-c", "x") is False


def test_reverse_lookup_index_to_tenant(db_connection):
    db = db_connection
    db.create_index_row("tenant-a", "vet", "t-tenant-a-vet")
    row = db.get_index_by_marqo_index("t-tenant-a-vet")
    assert row["instance"] == "tenant-a"
    assert db.get_index_by_marqo_index("does-not-exist") is None


# =============================================================================
# api.resolve_index / assert_index_access
# =============================================================================


def test_api_resolve_index_registry_and_fallback(db_connection, monkeypatch):
    monkeypatch.setattr(api, "db", db_mod)
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    # Registry hit
    assert api.resolve_index("tenant-a", "vet") == "t-tenant-a-vet"
    assert api.resolve_index("tenant-a") == "t-tenant-a-vet"
    # Unregistered tenant default -> legacy physical fallback (inert).
    assert api.resolve_index("ghost") == api._default_physical_index()
    # Named-but-unregistered index does not exist -> 404.
    with pytest.raises(HTTPException) as exc:
        api.resolve_index("tenant-a", "missing")
    assert exc.value.status_code == 404


def test_assert_index_access_denies_cross_tenant(db_connection, monkeypatch):
    monkeypatch.setattr(api, "db", db_mod)
    db_mod.create_index_row("tenant-b", "vet", "t-tenant-b-vet", is_default=True)
    viewer_a = _viewer_in("tenant-a")
    # Cross-tenant access is hidden as 404 (existence not leaked).
    with pytest.raises(HTTPException) as exc:
        api.assert_index_access(viewer_a, "tenant-b", "vet")
    assert exc.value.status_code == 404
    # Own tenant resolves fine.
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    assert api.assert_index_access(viewer_a, "tenant-a", "vet") == "t-tenant-a-vet"
    # Unrestricted admin passes anywhere.
    assert api.assert_index_access(_admin(), "tenant-b", "vet") == "t-tenant-b-vet"


def test_assert_index_access_named_unregistered_is_404(db_connection, monkeypatch):
    monkeypatch.setattr(api, "db", db_mod)
    with pytest.raises(HTTPException) as exc:
        api.assert_index_access(local_bypass_user(), "default", "nope")
    assert exc.value.status_code == 404


def test_assert_marqo_index_access_restricted_cannot_target_unregistered(db_connection, monkeypatch):
    monkeypatch.setattr(api, "db", db_mod)
    # A registered physical index reverse-resolves to its tenant.
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet")
    assert api.assert_marqo_index_access(_curator_in("tenant-a"), "t-tenant-a-vet") == "t-tenant-a-vet"
    # Another tenant's registered physical index is hidden.
    with pytest.raises(HTTPException) as exc:
        api.assert_marqo_index_access(_curator_in("tenant-a"), "t-tenant-b-vet")
    # unregistered physical index + restricted caller -> 404
    assert exc.value.status_code == 404
    # Unrestricted caller may still target an unregistered legacy index.
    assert api.assert_marqo_index_access(_admin(), "legacy-index") == "legacy-index"


# =============================================================================
# manage_indexes API — gating + multi-index
# =============================================================================


def _patch_marqo(monkeypatch):
    monkeypatch.setattr(api, "db", db_mod)
    monkeypatch.setattr(api, "_create_marqo_index_with_schema", MagicMock(return_value={}))
    fake_client = MagicMock()
    monkeypatch.setattr(api, "_marqo_client", lambda: fake_client)
    return fake_client


def test_create_index_gating_admin_in_tenant_allowed(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    # content_curator (pipeline) in tenant-a is an authorized self-service member.
    res = _run(api.create_tenant_index("tenant-a", {"name": "vet"}, _curator_in("tenant-a")))
    assert res["marqo_index"] == "t-tenant-a-vet"
    assert res["is_default"] is True  # first index for the tenant
    # A second index resolves to a distinct physical name and is not default.
    res2 = _run(api.create_tenant_index("tenant-a", {"name": "schemes"}, _curator_in("tenant-a")))
    assert res2["marqo_index"] == "t-tenant-a-schemes"
    assert res2["is_default"] is False
    assert res["marqo_index"] != res2["marqo_index"]


def test_create_index_gating_viewer_denied(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_index("tenant-a", {"name": "vet"}, _viewer_in("tenant-a")))
    assert exc.value.status_code == 403


def test_create_index_gating_other_tenant_denied_404(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    # curator in tenant-a cannot create in tenant-b; cross-tenant hidden as 404.
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_index("tenant-b", {"name": "vet"}, _curator_in("tenant-a")))
    assert exc.value.status_code == 404


def test_create_index_duplicate_conflicts(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, _curator_in("tenant-a")))
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_index("tenant-a", {"name": "vet"}, _curator_in("tenant-a")))
    assert exc.value.status_code == 409


def test_list_indexes_gated_to_tenant(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, _curator_in("tenant-a")))
    # Any access to the tenant may list (viewer ok).
    rows = _run(api.list_tenant_indexes("tenant-a", _viewer_in("tenant-a")))
    assert {r["name"] for r in rows} == {"vet"}
    # No access -> 404.
    with pytest.raises(HTTPException) as exc:
        _run(api.list_tenant_indexes("tenant-a", _viewer_in("tenant-b")))
    assert exc.value.status_code == 404


def test_delete_index_requires_admin_and_guards(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    admin = _admin()
    # Two indexes so the default guard is meaningful.
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, admin))       # default
    _run(api.create_tenant_index("tenant-a", {"name": "schemes"}, admin))   # non-default

    # Curator (pipeline but not admin) cannot delete -> 403.
    with pytest.raises(HTTPException) as exc:
        _run(api.delete_tenant_index("tenant-a", "schemes", _curator_in("tenant-a"), force=False))
    assert exc.value.status_code == 403

    # Deleting the tenant default while another index exists is refused (409).
    with pytest.raises(HTTPException) as exc:
        _run(api.delete_tenant_index("tenant-a", "vet", admin, force=False))
    assert exc.value.status_code == 409

    # Non-default with no documents deletes cleanly.
    out = _run(api.delete_tenant_index("tenant-a", "schemes", admin, force=False))
    assert out["name"] == "schemes"
    assert db_mod.get_index("tenant-a", "schemes") is None


def test_delete_index_with_documents_requires_force_and_reassigns(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    admin = _admin()
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, admin))       # default
    _run(api.create_tenant_index("tenant-a", {"name": "schemes"}, admin))   # non-default
    db_mod.upsert_document(
        workflow_id="wf-s", document_id="d-s", filename="s.pdf",
        filepath="/tmp/s.pdf", instance="tenant-a", index="schemes",
    )
    # Non-empty index refuses deletion without force.
    with pytest.raises(HTTPException) as exc:
        _run(api.delete_tenant_index("tenant-a", "schemes", admin, force=False))
    assert exc.value.status_code == 409
    # With force, its documents are reassigned to the tenant default (index=NULL).
    out = _run(api.delete_tenant_index("tenant-a", "schemes", admin, force=True))
    assert out["documents_reassigned"] == 1
    assert db_mod.get_document("wf-s")["index"] is None


# =============================================================================
# manage_tenants API
# =============================================================================


def test_create_tenant_provisions_default_index(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    out = _run(api.create_tenant_route({"instance": "tenant-x", "display_name": "Tenant X"}, _master_admin()))
    assert out["tenant"]["id"] == "tenant-x"
    assert out["default_index"]["marqo_index"] == "t-tenant-x-default"
    assert out["default_index"]["is_default"] is True
    # Registry now resolves the tenant default.
    assert db_mod.resolve_marqo_index("tenant-x") == "t-tenant-x-default"


def test_create_tenant_duplicate_conflicts(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    assert exc.value.status_code == 409


def test_suspend_and_delete_tenant(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    _run(api.create_tenant_index("tenant-x", {"name": "extra"}, _admin()))

    suspended = _run(api.suspend_tenant_route("tenant-x", _master_admin()))
    assert suspended["status"] == "suspended"

    # Destructive delete needs ?confirm.
    with pytest.raises(HTTPException) as exc:
        _run(api.delete_tenant_route("tenant-x", _master_admin(), confirm=False))
    assert exc.value.status_code == 400

    out = _run(api.delete_tenant_route("tenant-x", _master_admin(), confirm=True))
    # Both the default and the extra index registry rows are removed.
    assert out["registry_rows_removed"] == 2
    assert db_mod.get_tenant("tenant-x") is None
    assert db_mod.list_indexes("tenant-x") == []
