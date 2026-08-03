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
    """Control-plane platform admin — the realm ``master_admin`` role.

    Manages the tenant registry ONLY; holds no data permissions and is not a
    member of any tenant, so it cannot manage a tenant's indexes. For a caller
    that may manage indexes inside a tenant use ``_tenant_admin_in``; for a
    truly data-unrestricted caller use ``local_bypass_user`` (local dev).
    """
    return claims_to_user({"sub": "admin-1", "realm_access": {"roles": ["master_admin"]}})


def _master_admin():
    return claims_to_user({"sub": "root", "realm_access": {"roles": ["master_admin"]}})


def _tenant_admin_in(instance: str):
    """Tenant-scoped admin: holds ``admin`` inside ``instance`` only (not platform-wide)."""
    return claims_to_user({"sub": "tadmin", "tenant_roles": {instance: ["admin"]}})


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


def test_ensure_tenant_default_index_provisions_own_namespace(db_connection):
    db = db_connection
    # A fresh tenant with no index -> the ingest path must land in the tenant's OWN
    # namespace (`<ns><instance>-default`), NEVER the legacy/default-tenant index.
    physical = db.ensure_tenant_default_index("tenant-new")
    assert physical == "t-tenant-new-default"
    assert physical != db._default_physical_index()
    # It is now the registered tenant default (idempotent on a second call).
    assert db.resolve_marqo_index("tenant-new") == "t-tenant-new-default"
    assert db.ensure_tenant_default_index("tenant-new") == "t-tenant-new-default"
    assert len(db.list_indexes("tenant-new")) == 1
    # A named (non-default) logical index provisions its own physical name.
    named = db.ensure_tenant_default_index("tenant-new", "schemes")
    assert named == "t-tenant-new-schemes"
    assert bool(db.get_index("tenant-new", "schemes")["is_default"]) is False


def test_ensure_tenant_default_index_default_instance_uses_legacy(db_connection):
    db = db_connection
    default = db._default_instance_id()
    # The DEFAULT single-tenant instance keeps writing to the legacy physical index.
    assert db.ensure_tenant_default_index(default) == db._default_physical_index()


def test_reverse_lookup_index_to_tenant(db_connection):
    db = db_connection
    db.create_index_row("tenant-a", "vet", "t-tenant-a-vet")
    row = db.get_index_by_marqo_index("t-tenant-a-vet")
    assert row["instance"] == "tenant-a"
    assert db.get_index_by_marqo_index("does-not-exist") is None


# =============================================================================
# api.resolve_index / assert_index_access
# =============================================================================


def test_api_resolve_index_registry_and_no_cross_tenant_fallback(db_connection, monkeypatch):
    monkeypatch.setattr(api, "db", db_mod)
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    # Registry hit
    assert api.resolve_index("tenant-a", "vet") == "t-tenant-a-vet"
    assert api.resolve_index("tenant-a") == "t-tenant-a-vet"
    # A non-default tenant with NO registered index resolves to None — it has no
    # index. It must NEVER fall back to the default tenant's physical index (that
    # would leak the default tenant's documents to another tenant on a READ).
    assert api.resolve_index("ghost") is None
    # A registered-but-index-less tenant is likewise None.
    db_mod.create_tenant_row("tenant-x", display_name="Tenant X")
    assert api.resolve_index("tenant-x") is None
    # Named-but-unregistered index does not exist -> 404.
    with pytest.raises(HTTPException) as exc:
        api.resolve_index("tenant-a", "missing")
    assert exc.value.status_code == 404


def test_api_resolve_index_default_instance_legacy_backcompat(db_connection, monkeypatch):
    """The DEFAULT instance keeps its legacy single-tenant back-compat: with no
    registered default it maps to the legacy physical index. This is the ONE
    instance allowed to use ``_default_physical_index()`` on a name=None miss."""
    monkeypatch.setattr(api, "db", db_mod)
    default = db_mod._default_instance_id()
    # Seeded registry maps default -> legacy physical.
    assert api.resolve_index(default) == api._default_physical_index()
    # Even with an EMPTY registry (drop the seeded default row) the default
    # instance still resolves to the legacy physical index (back-compat).
    db_mod.delete_index_row(default, "default")
    assert db_mod.get_default_index(default) is None
    assert api.resolve_index(default) == api._default_physical_index()


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
    # A data-unrestricted caller (local bypass) passes anywhere. A control-plane
    # master_admin does NOT — it is not a member of tenant-b (asserted below).
    assert api.assert_index_access(local_bypass_user(), "tenant-b", "vet") == "t-tenant-b-vet"
    with pytest.raises(HTTPException) as exc:
        api.assert_index_access(_admin(), "tenant-b", "vet")
    assert exc.value.status_code == 404


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
    # A data-unrestricted caller (local bypass) may still target an unregistered
    # legacy index; a control-plane master_admin (restricted data scope) cannot.
    assert api.assert_marqo_index_access(local_bypass_user(), "legacy-index") == "legacy-index"
    with pytest.raises(HTTPException) as exc:
        api.assert_marqo_index_access(_admin(), "legacy-index")
    assert exc.value.status_code == 404


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


def test_master_admin_cannot_manage_tenant_indexes(db_connection, monkeypatch):
    """Managing a tenant's indexes is a tenant (data-plane) operation. The
    control-plane ``master_admin`` is not a member of the tenant, so create /
    delete are denied with 403 (it may create the tenant + its DEFAULT index via
    POST /tenants, but not manage indexes thereafter)."""
    _patch_marqo(monkeypatch)
    # Seed a tenant with a couple of indexes via a tenant admin.
    tadmin = _tenant_admin_in("tenant-a")
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, tadmin))
    _run(api.create_tenant_index("tenant-a", {"name": "schemes"}, tadmin))

    master = _master_admin()
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_index("tenant-a", {"name": "extra"}, master))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        _run(api.delete_tenant_index("tenant-a", "schemes", master, force=False))
    assert exc.value.status_code == 403
    # Listing a tenant's indexes is likewise data-plane -> 403 for master_admin.
    with pytest.raises(HTTPException) as exc:
        _run(api.list_tenant_indexes("tenant-a", master))
    assert exc.value.status_code == 403


def test_create_index_duplicate_conflicts(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, _curator_in("tenant-a")))
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_index("tenant-a", {"name": "vet"}, _curator_in("tenant-a")))
    assert exc.value.status_code == 409


# =============================================================================
# H2 — physical index name collision / cross-tenant destruction guards
# =============================================================================


def test_new_marqo_index_name_rejects_dash_in_name():
    """A logical name containing '-' is rejected (400) so it cannot alias another
    (instance, name) via the '-' that joins instance and name."""
    with pytest.raises(HTTPException) as exc:
        api._new_marqo_index_name("tenant-a", "foo-bar")
    assert exc.value.status_code == 400
    # Underscores/digits are fine, and the physical name uses a single '-' join.
    assert api._new_marqo_index_name("tenant-a", "vet_2024") == "t-tenant-a-vet_2024"


def test_create_index_name_with_dash_rejected(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_index("tenant-a", {"name": "vet-schemes"}, _curator_in("tenant-a")))
    assert exc.value.status_code == 400


def test_create_tenant_physical_index_collision_409(db_connection, monkeypatch):
    """create_tenant must 409 (not adopt) when its default physical index name is
    already registered to another tenant — and leave no orphan tenant row."""
    _patch_marqo(monkeypatch)
    # Another tenant already owns the physical name tenant-x's default would compute.
    db_mod.create_index_row(
        instance="other", name="default", marqo_index="t-tenant-x-default", is_default=True,
    )
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    assert exc.value.status_code == 409
    # No orphan tenant row was written (guard runs before db.create_tenant).
    assert db_mod.get_tenant("tenant-x") is None


def test_create_marqo_index_refuses_to_adopt_foreign_physical(db_connection, monkeypatch):
    """_create_marqo_index_with_schema must 409 when the physical index already
    exists in Marqo but is not this tenant's registered index (no silent adoption)."""
    monkeypatch.setattr(api, "db", db_mod)

    class _ExistingClient:
        def get_index(self, name):
            return {"name": name}  # physically exists

        def create_index(self, name, settings_dict=None):
            raise AssertionError("must not create/adopt a pre-existing physical index")

    monkeypatch.setattr(api, "_marqo_client", lambda: _ExistingClient())
    with pytest.raises(HTTPException) as exc:
        api._create_marqo_index_with_schema("t-tenant-a-vet")
    assert exc.value.status_code == 409

    # Once it IS registered to a tenant, re-create is an idempotent no-op (returns settings).
    db_mod.create_index_row(instance="tenant-a", name="vet", marqo_index="t-tenant-a-vet")
    settings = api._create_marqo_index_with_schema("t-tenant-a-vet")
    assert isinstance(settings, dict)


def test_create_index_refuses_non_empty_orphan_physical_index(db_connection, monkeypatch):
    """A physical index left behind by an earlier delete must not be re-adopted:
    creating the same logical name over a non-empty collection is a 409."""
    fake = _patch_marqo(monkeypatch)
    fake.index.return_value.get_stats.return_value = {"numberOfDocuments": 7}
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_index("tenant-a", {"name": "vet"}, _curator_in("tenant-a")))
    assert exc.value.status_code == 409
    assert "7" in exc.value.detail
    assert db_mod.get_index("tenant-a", "vet") is None  # no registry row written

    # An empty (or absent) physical index is not stale vectors -> create proceeds.
    fake.index.return_value.get_stats.return_value = {"numberOfDocuments": 0}
    out = _run(api.create_tenant_index("tenant-a", {"name": "vet"}, _curator_in("tenant-a")))
    assert out["marqo_index"] == "t-tenant-a-vet"


def test_delete_then_recreate_refuses_stale_physical_index(db_connection, monkeypatch):
    """Full cycle: the Marqo drop fails during delete (registry row goes anyway),
    so the physical collection outlives the index — re-creating it is refused."""
    fake = _patch_marqo(monkeypatch)
    admin = _tenant_admin_in("tenant-a")
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, admin))       # default
    _run(api.create_tenant_index("tenant-a", {"name": "schemes"}, admin))   # non-default
    fake.delete_index.side_effect = RuntimeError("marqo unreachable")
    out = _run(api.delete_tenant_index("tenant-a", "schemes", admin, force=False))
    assert out["marqo_dropped"] is False
    assert db_mod.get_index("tenant-a", "schemes") is None

    # The orphaned collection still holds the old vectors.
    fake.index.return_value.get_stats.return_value = {"numberOfDocuments": 12}
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_index("tenant-a", {"name": "schemes"}, admin))
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
    # Index management is a tenant (data-plane) operation -> a tenant admin, not
    # the control-plane master_admin.
    admin = _tenant_admin_in("tenant-a")
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
    admin = _tenant_admin_in("tenant-a")
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
# update_tenant_index API — rename / set-default / guards
# =============================================================================


def test_update_index_rename_display_name(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    admin = _tenant_admin_in("tenant-a")
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, admin))
    out = _run(api.update_tenant_index("tenant-a", "vet", {"display_name": "  Veterinary  "}, admin))
    assert out["display_name"] == "Veterinary"
    assert db_mod.get_index("tenant-a", "vet")["display_name"] == "Veterinary"
    # Empty string clears the cosmetic label back to NULL (name is untouched).
    out = _run(api.update_tenant_index("tenant-a", "vet", {"display_name": ""}, admin))
    assert out["display_name"] is None
    assert out["name"] == "vet"


def test_update_index_set_default_flips_previous(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    admin = _tenant_admin_in("tenant-a")
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, admin))       # default
    _run(api.create_tenant_index("tenant-a", {"name": "schemes"}, admin))   # non-default
    out = _run(api.update_tenant_index("tenant-a", "schemes", {"is_default": True}, admin))
    assert out["is_default"] is True
    defaults = [r["name"] for r in db_mod.list_indexes("tenant-a") if r["is_default"]]
    assert defaults == ["schemes"]  # exactly one default; the old one flipped off


def test_update_index_cannot_unset_only_default(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    admin = _tenant_admin_in("tenant-a")
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, admin))       # default
    _run(api.create_tenant_index("tenant-a", {"name": "schemes"}, admin))   # non-default
    # Unsetting the current default would leave the tenant with none -> 409.
    with pytest.raises(HTTPException) as exc:
        _run(api.update_tenant_index("tenant-a", "vet", {"is_default": False}, admin))
    assert exc.value.status_code == 409
    assert bool(db_mod.get_index("tenant-a", "vet")["is_default"]) is True
    # is_default:false on a NON-default index changes nothing, so it is a 400 rather
    # than a 200 the caller would read as a completed demotion.
    with pytest.raises(HTTPException) as exc:
        _run(api.update_tenant_index("tenant-a", "schemes", {"is_default": False}, admin))
    assert exc.value.status_code == 400
    assert bool(db_mod.get_index("tenant-a", "schemes")["is_default"]) is False


def test_update_index_embedding_model_in_use_guard(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    admin = _tenant_admin_in("tenant-a")
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, admin))       # default
    _run(api.create_tenant_index("tenant-a", {"name": "schemes"}, admin))   # non-default
    db_mod.upsert_document(
        workflow_id="wf-e", document_id="d-e", filename="e.pdf",
        filepath="/tmp/e.pdf", instance="tenant-a", index="schemes",
    )
    # Breaking change (embedding model) on an in-use index is refused without force.
    with pytest.raises(HTTPException) as exc:
        _run(api.update_tenant_index("tenant-a", "schemes", {"embedding_model": "new-model"}, admin, force=False))
    assert exc.value.status_code == 409
    assert db_mod.get_index("tenant-a", "schemes")["embedding_model"] is None
    # With force it applies (documents must be reindexed afterwards).
    out = _run(api.update_tenant_index("tenant-a", "schemes", {"embedding_model": "new-model"}, admin, force=True))
    assert out["embedding_model"] == "new-model"
    # A rename on that same in-use index needs no force (cosmetic, non-breaking).
    out = _run(api.update_tenant_index("tenant-a", "schemes", {"display_name": "Schemes"}, admin, force=False))
    assert out["display_name"] == "Schemes"


def test_forced_embedding_change_flags_documents_for_reindex(db_connection, monkeypatch):
    """A forced embedding-model change invalidates every vector already in the
    index, so its documents must come back flagged ``reindex_required`` —
    otherwise the stale set is unfindable and the index silently disagrees with
    every document in it."""
    _patch_marqo(monkeypatch)
    admin = _tenant_admin_in("tenant-a")
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, admin))       # default
    _run(api.create_tenant_index("tenant-a", {"name": "schemes"}, admin))   # non-default
    for i in range(3):
        db_mod.upsert_document(
            workflow_id=f"wf-{i}", document_id=f"d-{i}", filename=f"{i}.pdf",
            filepath=f"/tmp/{i}.pdf", instance="tenant-a", index="schemes",
        )
    # A document in the OTHER index must not be dragged in.
    db_mod.upsert_document(
        workflow_id="wf-other", document_id="d-other", filename="o.pdf",
        filepath="/tmp/o.pdf", instance="tenant-a", index="vet",
    )
    assert all(not db_mod.get_document(f"wf-{i}")["reindex_required"] for i in range(3))

    out = _run(api.update_tenant_index(
        "tenant-a", "schemes", {"embedding_model": "new-model"}, admin, force=True,
    ))
    assert out["embedding_model"] == "new-model"
    assert out["documents_flagged_for_reindex"] == 3
    for i in range(3):
        doc = db_mod.get_document(f"wf-{i}")
        assert bool(doc["reindex_required"]) is True
        assert "new-model" in (doc["reindex_reason"] or "")
    assert bool(db_mod.get_document("wf-other")["reindex_required"]) is False

    # A cosmetic patch flags nothing (only the breaking change sweeps).
    out = _run(api.update_tenant_index("tenant-a", "schemes", {"display_name": "S"}, admin))
    assert out["documents_flagged_for_reindex"] == 0


def test_forced_embedding_change_on_default_flags_unassigned_documents(db_connection, monkeypatch):
    """Documents with a NULL ``index`` resolve to the tenant default, so a forced
    change on the DEFAULT index must flag them too."""
    _patch_marqo(monkeypatch)
    admin = _tenant_admin_in("tenant-a")
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, admin))  # default
    db_mod.upsert_document(
        workflow_id="wf-null", document_id="d-null", filename="n.pdf",
        filepath="/tmp/n.pdf", instance="tenant-a",  # index NULL -> tenant default
    )
    out = _run(api.update_tenant_index(
        "tenant-a", "vet", {"embedding_model": "new-model"}, admin, force=True,
    ))
    assert out["documents_flagged_for_reindex"] == 1
    assert bool(db_mod.get_document("wf-null")["reindex_required"]) is True


def test_update_index_404s_when_deleted_between_read_and_write(db_connection, monkeypatch):
    """The route's 404 comes from an earlier ``get_index`` read; a delete landing
    between that read and the write must not commit silently and return 200."""
    _patch_marqo(monkeypatch)
    admin = _tenant_admin_in("tenant-a")
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, admin))
    real_update = db_mod.update_index

    def _racing_update(instance, name, **kwargs):
        db_mod.delete_index_row(instance, name)  # concurrent delete
        return real_update(instance, name, **kwargs)

    monkeypatch.setattr(db_mod, "update_index", _racing_update)
    with pytest.raises(HTTPException) as exc:
        _run(api.update_tenant_index("tenant-a", "vet", {"display_name": "X"}, admin))
    assert exc.value.status_code == 404


def test_update_index_gating_curator_denied_and_cross_tenant_404(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    admin = _tenant_admin_in("tenant-a")
    _run(api.create_tenant_index("tenant-a", {"name": "vet"}, admin))
    # Curator (pipeline, not admin) may create but not modify -> 403.
    with pytest.raises(HTTPException) as exc:
        _run(api.update_tenant_index("tenant-a", "vet", {"display_name": "X"}, _curator_in("tenant-a")))
    assert exc.value.status_code == 403
    # Cross-tenant caller is hidden as 404 (existence not leaked).
    with pytest.raises(HTTPException) as exc:
        _run(api.update_tenant_index("tenant-a", "vet", {"display_name": "X"}, _tenant_admin_in("tenant-b")))
    assert exc.value.status_code == 404
    # Unknown index for an authorized admin -> 404.
    with pytest.raises(HTTPException) as exc:
        _run(api.update_tenant_index("tenant-a", "ghost", {"display_name": "X"}, admin))
    assert exc.value.status_code == 404


def test_db_update_index_and_set_default(db_connection):
    db = db_connection
    db.create_index_row("tenant-d", "a", "t-tenant-d-a", is_default=True)
    db.create_index_row("tenant-d", "b", "t-tenant-d-b")
    # Partial update: only display_name written, embedding_model left untouched.
    row = db.update_index("tenant-d", "a", display_name="Alpha")
    assert row["display_name"] == "Alpha"
    assert row["embedding_model"] is None
    # An UPDATE matching no row is reported (rowcount-checked), never committed silently.
    assert db.update_index("tenant-d", "ghost", display_name="X") is None
    assert db.update_index("ghost-tenant", "a", display_name="X") is None
    # set_default_index flips the sole default; unknown index returns None.
    assert db.set_default_index("tenant-d", "b")["is_default"] == 1
    assert [r["name"] for r in db.list_indexes("tenant-d") if r["is_default"]] == ["b"]
    assert db.set_default_index("tenant-d", "nope") is None


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


def test_create_tenant_duplicate_adopts(db_connection, monkeypatch):
    """Creating an already-existing instance is now idempotent: it ADOPTS the
    existing tenant (adopted=True) instead of 409-ing, and does NOT create a
    second default Marqo index."""
    _patch_marqo(monkeypatch)
    first = _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    assert first["adopted"] is False
    marqo_calls_before = api._create_marqo_index_with_schema.call_count

    second = _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    assert second["adopted"] is True
    assert second["default_index"]["marqo_index"] == "t-tenant-x-default"
    # No duplicate physical index provisioned on adopt.
    assert api._create_marqo_index_with_schema.call_count == marqo_calls_before
    # Still exactly one index registered for the tenant.
    assert len(db_mod.list_indexes("tenant-x")) == 1


def test_suspend_and_delete_tenant(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    # An additional index within the tenant is created by a tenant admin (the
    # master_admin only created the tenant + its default index).
    _run(api.create_tenant_index("tenant-x", {"name": "extra"}, _tenant_admin_in("tenant-x")))

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
