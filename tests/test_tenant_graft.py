"""Graft-specific invariants: registry fail-closed, membership, tenant_api guards.

Complements test_tenancy.py (which covers the pre-graft tenancy-v0 scoping). These
assert the NEW multi-tenancy graft behavior: collection-per-tenant resolution that
never falls back cross-tenant, the app-side membership store, and the provisioning
route guards (404 hide / 403 wrong-role / platform-admin short-circuit).
"""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

from pipeline.auth.jwt import claims_to_user
from pipeline.auth.models import local_bypass_user


def _default_instance() -> str:
    return (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower() or "default"


# --------------------------------------------------------------------------- #
# Registry: fail-closed resolution + never-cross-tenant provisioning
# --------------------------------------------------------------------------- #

def test_resolve_index_is_fail_closed_for_unknown_tenant(db_connection):
    db = db_connection
    # A tenant with no registered index resolves to None — NOT another tenant's
    # index and NOT the default/legacy collection.
    assert db.resolve_index("tenant-with-no-index") is None
    assert db.resolve_index("tenant-with-no-index", "some-logical") is None


def test_default_tenant_resolves_to_legacy_collection(db_connection):
    db = db_connection
    # The seed registers the live collection as (DEFAULT_INSTANCE, 'default').
    from pipeline.vector_store import get_default_index_name

    resolved = db.resolve_index(_default_instance())
    assert resolved == get_default_index_name()


def test_ensure_tenant_default_index_never_cross_tenant(db_connection):
    db = db_connection
    a = db.ensure_tenant_default_index("tenant-a")
    b = db.ensure_tenant_default_index("tenant-b")
    assert a != b
    # Each lands in its OWN namespace, never the legacy/default collection.
    from pipeline.vector_store import get_default_index_name

    legacy = get_default_index_name()
    assert a != legacy and b != legacy
    assert "tenant-a" in a and "tenant-b" in b
    # Idempotent + now resolvable.
    assert db.ensure_tenant_default_index("tenant-a") == a
    assert db.resolve_index("tenant-a") == a


def test_physical_name_uses_namespace_prefix(db_connection, monkeypatch):
    monkeypatch.setenv("QDRANT_COLLECTION_NAMESPACE", "t-")
    db = db_connection
    name = db.ensure_tenant_default_index("tenant-x")
    assert name.startswith("t-tenant-x")


# --------------------------------------------------------------------------- #
# App-side membership store (D2 baseline)
# --------------------------------------------------------------------------- #

def test_membership_add_list_and_roles(db_connection):
    db = db_connection
    assert db.add_tenant_member("u1", "tenant-a", "state_admin", added_by="root") is not None
    assert db.add_tenant_member("u1", "tenant-a", "viewer", added_by="root") is not None
    roles = db.get_tenant_roles_for_user("u1")
    assert roles.get("tenant-a") == {"state_admin", "viewer"}
    members = db.list_tenant_members("tenant-a")
    assert {m["role"] for m in members} == {"state_admin", "viewer"}


def test_membership_rejects_platform_and_unknown_roles(db_connection):
    db = db_connection
    # superadmin/master_admin are platform-level and must NOT be assignable as a
    # tenant membership (else a per-tenant grant would escalate platform-wide).
    assert db.add_tenant_member("u2", "tenant-a", "superadmin", added_by="root") is None
    assert db.add_tenant_member("u2", "tenant-a", "nonsense", added_by="root") is None
    assert db.get_tenant_roles_for_user("u2") == {}


def test_membership_overlays_into_auth(db_connection):
    """A token with NO tenant claim still gets roles from the app-side store."""
    db = db_connection
    db.add_tenant_member("u3", "tenant-a", "state_admin", added_by="root")
    user = claims_to_user({"sub": "u3"})
    # roles_in unions claim (none) + DB membership.
    assert "state_admin" in user.roles_in("tenant-a")
    assert user.roles_in("tenant-b") == set()


# --------------------------------------------------------------------------- #
# tenant_api guards: platform short-circuit / member view / 404 hide / 403 role
# --------------------------------------------------------------------------- #

def _superadmin():
    return claims_to_user({"sub": "root", "realm_access": {"roles": ["superadmin"]}})


def _tenant_user(instance, role):
    return claims_to_user({"sub": f"{role}@{instance}", "tenant_roles": {instance: [role]}})


def test_view_guard_hides_nonmember_as_404(db_connection):
    from pipeline.tenant_api import _assert_can_view_tenant

    outsider = _tenant_user("tenant-a", "viewer")
    with pytest.raises(HTTPException) as exc:
        _assert_can_view_tenant(outsider, "tenant-b")
    assert exc.value.status_code == 404
    # own tenant is viewable
    _assert_can_view_tenant(outsider, "tenant-a")
    # platform admin sees any tenant
    _assert_can_view_tenant(_superadmin(), "tenant-b")
    _assert_can_view_tenant(local_bypass_user(), "tenant-b")


def test_manage_guard_403_for_reachable_wrong_role(db_connection):
    from pipeline.tenant_api import _assert_can_manage_members

    viewer = _tenant_user("tenant-a", "viewer")
    # viewer can reach tenant-a but cannot manage members there -> 403 (not 404).
    with pytest.raises(HTTPException) as exc:
        _assert_can_manage_members(viewer, "tenant-a")
    assert exc.value.status_code == 403
    # a state_admin in the tenant can manage.
    _assert_can_manage_members(_tenant_user("tenant-a", "state_admin"), "tenant-a")
    # non-member is hidden as 404.
    with pytest.raises(HTTPException) as exc2:
        _assert_can_manage_members(viewer, "tenant-b")
    assert exc2.value.status_code == 404
    # platform admin manages anything.
    _assert_can_manage_members(_superadmin(), "tenant-b")


def test_manage_indexes_guard_matches_manage_semantics(db_connection):
    from pipeline.tenant_api import _assert_can_manage_indexes

    _assert_can_manage_indexes(_tenant_user("tenant-a", "state_admin"), "tenant-a")
    with pytest.raises(HTTPException) as exc:
        _assert_can_manage_indexes(_tenant_user("tenant-a", "content_curator"), "tenant-a")
    assert exc.value.status_code == 403
