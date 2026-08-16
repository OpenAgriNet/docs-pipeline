"""Tests for per-tenant tag taxonomy management (Phase 5, surface P5).

Covers the DB-backed per-tenant taxonomy (seed-from-default, node CRUD, tenant
isolation), the management API's auth discipline (403/404 via
``_assert_tenant_scope`` / ``Permission.ADMIN``, not ``MANAGE_USERS``), and the
tenant-scoped loader that feeds domain tagging.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from pipeline.auth.jwt import claims_to_user
from pipeline.domain_tags.base import load_taxonomy, load_taxonomy_for_instance
from pipeline.routers import search as search_routes
from pipeline.routers import tenants as tenant_routes


def _run(coro):
    return asyncio.run(coro)


# --- principals --------------------------------------------------------------


def _platform_admin():
    return claims_to_user({"sub": "root", "realm_access": {"roles": ["master_admin"]}})


def _tenant_admin_in(instance: str):
    return claims_to_user({"sub": "tadmin", "tenant_roles": {instance: ["admin"]}})


def _curator_in(instance: str):
    return claims_to_user({"sub": "cur", "tenant_roles": {instance: ["content_curator"]}})


def _viewer_in(instance: str):
    return claims_to_user({"sub": "view", "tenant_roles": {instance: ["viewer"]}})


# =============================================================================
# DB layer: seed from default + node CRUD
# =============================================================================


def test_default_tenant_seeded_from_taxonomy_json(db_connection):
    """init_db seeds the default tenant's taxonomy from the shipped file."""
    db = db_connection
    taxonomy = db.get_taxonomy("default")
    assert taxonomy is not None
    file_default = load_taxonomy()
    # Every non-empty dimension's vocabulary matches the file (as a set).
    for domain, dims in file_default["domains"].items():
        for dimension, values in dims.items():
            seeded = taxonomy["domains"][domain][dimension]
            assert set(seeded) == {v.strip() for v in values}


def test_empty_dimension_survives_roundtrip(db_connection):
    """An empty vocabulary (e.g. ``crop: []``) is preserved, not dropped."""
    db = db_connection
    taxonomy = db.get_taxonomy("default")
    # crop.* dimensions ship empty in the default taxonomy.
    assert taxonomy["domains"]["crop"]["crop"] == []
    assert "state" in taxonomy["domains"]["cross_cutting"]
    assert taxonomy["domains"]["cross_cutting"]["state"] == []


def test_seed_is_idempotent(db_connection):
    """Re-seeding a tenant that already has rows is a no-op (curation-safe)."""
    db = db_connection
    before = len(db.list_taxonomy_nodes("default"))
    # A distinct edit that must not be clobbered by a re-seed.
    db.add_taxonomy_node("default", "animal_husbandry", "species", "llama")
    seeded_again = db.seed_taxonomy_for_instance("default", load_taxonomy())
    assert seeded_again is False
    nodes = db.list_taxonomy_nodes("default")
    assert len(nodes) == before + 1
    assert any(n["value"] == "llama" for n in nodes)


def test_add_node_new_and_duplicate(db_connection):
    db = db_connection
    row = db.add_taxonomy_node("tenant-a", "animal_husbandry", "species", "yak")
    assert row is not None
    assert row["domain"] == "animal_husbandry"
    assert row["value"] == "yak"
    # Duplicate -> None (route maps to 409).
    assert db.add_taxonomy_node("tenant-a", "animal_husbandry", "species", "yak") is None


def test_add_value_drops_empty_dimension_placeholder(db_connection):
    db = db_connection
    # An empty dimension placeholder...
    db.add_taxonomy_node("tenant-a", "crop", "crop", "")
    tax0 = db.get_taxonomy("tenant-a")
    assert tax0["domains"]["crop"]["crop"] == []
    # ...is replaced (not duplicated) when a real value arrives.
    db.add_taxonomy_node("tenant-a", "crop", "crop", "wheat")
    tax1 = db.get_taxonomy("tenant-a")
    assert tax1["domains"]["crop"]["crop"] == ["wheat"]


def test_add_node_preserves_value_casing(db_connection):
    db = db_connection
    row = db.add_taxonomy_node("tenant-a", "animal_husbandry", "breed", "Zebu")
    assert row["value"] == "Zebu"
    # domain/dimension are normalized to lowercase structural keys.
    row2 = db.add_taxonomy_node("tenant-a", "Animal_Husbandry", "Breed", "Nili")
    assert row2["domain"] == "animal_husbandry"
    assert row2["dimension"] == "breed"


def test_rename_node(db_connection):
    db = db_connection
    db.add_taxonomy_node("tenant-a", "animal_husbandry", "species", "yak")
    updated = db.rename_taxonomy_node("tenant-a", "animal_husbandry", "species", "yak", "dzo")
    assert updated is not None
    assert updated["value"] == "dzo"
    # Original gone; new present.
    values = {n["value"] for n in db.list_taxonomy_nodes("tenant-a")}
    assert "yak" not in values
    assert "dzo" in values


def test_rename_missing_returns_none(db_connection):
    db = db_connection
    db.add_taxonomy_node("tenant-a", "animal_husbandry", "species", "yak")
    assert db.rename_taxonomy_node("tenant-a", "animal_husbandry", "species", "ghost", "x") is None


def test_rename_onto_existing_value_raises(db_connection):
    db = db_connection
    db.add_taxonomy_node("tenant-a", "animal_husbandry", "species", "yak")
    db.add_taxonomy_node("tenant-a", "animal_husbandry", "species", "ox")
    with pytest.raises(ValueError):
        db.rename_taxonomy_node("tenant-a", "animal_husbandry", "species", "yak", "ox")


def test_delete_node(db_connection):
    db = db_connection
    db.add_taxonomy_node("tenant-a", "animal_husbandry", "species", "yak")
    assert db.delete_taxonomy_node("tenant-a", "animal_husbandry", "species", "yak") is True
    assert db.delete_taxonomy_node("tenant-a", "animal_husbandry", "species", "yak") is False


# =============================================================================
# Per-tenant isolation
# =============================================================================


def test_taxonomy_edit_is_tenant_isolated(db_connection):
    """Editing tenant-a's taxonomy never touches tenant-b's."""
    db = db_connection
    db.seed_taxonomy_for_instance("tenant-a", load_taxonomy())
    db.seed_taxonomy_for_instance("tenant-b", load_taxonomy())

    db.add_taxonomy_node("tenant-a", "animal_husbandry", "species", "llama")
    db.delete_taxonomy_node("tenant-a", "animal_husbandry", "species", "goat")

    a = db.get_taxonomy("tenant-a")["domains"]["animal_husbandry"]["species"]
    b = db.get_taxonomy("tenant-b")["domains"]["animal_husbandry"]["species"]
    assert "llama" in a and "goat" not in a
    assert "llama" not in b and "goat" in b


def test_loader_reflects_tenant_taxonomy_for_tagging(db_connection):
    """The domain-tagging loader resolves the caller's tenant taxonomy."""
    db = db_connection
    db.seed_taxonomy_for_instance("tenant-a", load_taxonomy())
    db.add_taxonomy_node("tenant-a", "animal_husbandry", "species", "llama")

    a = load_taxonomy_for_instance("tenant-a")
    assert "llama" in a["domains"]["animal_husbandry"]["species"]
    # An unseeded tenant transparently falls back to the shipped file default.
    b = load_taxonomy_for_instance("tenant-b")
    assert "llama" not in b["domains"]["animal_husbandry"]["species"]
    assert set(b["domains"]["animal_husbandry"]["species"]) == {"cattle", "buffalo", "goat"}


# =============================================================================
# Management API: auth discipline (tenant ADMIN + platform admin allowed)
# =============================================================================


def test_platform_admin_manages_any_tenant(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _platform_admin()

    created = _run(tenant_routes.create_tenant_taxonomy_node_route(
        "tenant-a", {"domain": "animal_husbandry", "dimension": "species", "value": "llama"}, admin
    ))
    assert created["value"] == "llama"

    taxonomy = _run(tenant_routes.get_tenant_taxonomy_route("tenant-a", admin))
    assert "llama" in taxonomy["domains"]["animal_husbandry"]["species"]


def test_tenant_admin_manages_own_taxonomy(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")

    created = _run(tenant_routes.create_tenant_taxonomy_node_route(
        "tenant-a", {"domain": "animal_husbandry", "dimension": "species", "value": "llama"}, admin
    ))
    assert created["value"] == "llama"

    renamed = _run(tenant_routes.rename_tenant_taxonomy_node_route(
        "tenant-a",
        {"domain": "animal_husbandry", "dimension": "species", "value": "llama", "new_value": "alpaca"},
        admin,
    ))
    assert renamed["value"] == "alpaca"

    deleted = _run(tenant_routes.delete_tenant_taxonomy_node_route(
        "tenant-a", admin, domain="animal_husbandry", dimension="species", value="alpaca"
    ))
    assert deleted["deleted"] is True


def test_create_duplicate_node_409(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")
    body = {"domain": "animal_husbandry", "dimension": "species", "value": "llama"}
    _run(tenant_routes.create_tenant_taxonomy_node_route("tenant-a", body, admin))
    with pytest.raises(HTTPException) as exc:
        _run(tenant_routes.create_tenant_taxonomy_node_route("tenant-a", body, admin))
    assert exc.value.status_code == 409


def test_rename_missing_node_404(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")
    with pytest.raises(HTTPException) as exc:
        _run(tenant_routes.rename_tenant_taxonomy_node_route(
            "tenant-a",
            {"domain": "animal_husbandry", "dimension": "species", "value": "ghost", "new_value": "x"},
            admin,
        ))
    assert exc.value.status_code == 404


def test_delete_missing_node_404(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")
    with pytest.raises(HTTPException) as exc:
        _run(tenant_routes.delete_tenant_taxonomy_node_route(
            "tenant-a", admin, domain="animal_husbandry", dimension="species", value="ghost"
        ))
    assert exc.value.status_code == 404


def test_tenant_admin_cannot_manage_other_tenant_404(db_connection):
    """Cross-tenant taxonomy management is hidden as 404 (never 403)."""
    db = db_connection
    db.create_tenant("tenant-a")
    db.create_tenant("tenant-b")
    admin_a = _tenant_admin_in("tenant-a")

    with pytest.raises(HTTPException) as exc:
        _run(tenant_routes.get_tenant_taxonomy_route("tenant-b", admin_a))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc2:
        _run(tenant_routes.create_tenant_taxonomy_node_route(
            "tenant-b", {"domain": "animal_husbandry", "dimension": "species", "value": "x"}, admin_a
        ))
    assert exc2.value.status_code == 404


def test_unknown_tenant_404(db_connection):
    admin = _platform_admin()
    with pytest.raises(HTTPException) as exc:
        _run(tenant_routes.get_tenant_taxonomy_route("ghost-tenant", admin))
    assert exc.value.status_code == 404


def test_viewer_and_curator_cannot_manage_taxonomy_403(db_connection):
    """A member with an insufficient role gets 403 (not 404)."""
    db = db_connection
    db.create_tenant("tenant-a")
    for principal in (_viewer_in("tenant-a"), _curator_in("tenant-a")):
        with pytest.raises(HTTPException) as exc:
            _run(tenant_routes.get_tenant_taxonomy_route("tenant-a", principal))
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc2:
            _run(tenant_routes.create_tenant_taxonomy_node_route(
                "tenant-a", {"domain": "animal_husbandry", "dimension": "species", "value": "x"}, principal
            ))
        assert exc2.value.status_code == 403


def test_taxonomy_auth_is_admin_not_manage_users(db_connection, monkeypatch):
    """Taxonomy gates on Permission.ADMIN, not MANAGE_USERS (member management)."""
    from pipeline.auth.models import AuthUser
    from pipeline.auth.permissions import Permission

    db = db_connection
    db.create_tenant("tenant-a")
    principal = _tenant_admin_in("tenant-a")

    monkeypatch.setattr(
        AuthUser,
        "permissions_in",
        lambda self, instance: {Permission.MANAGE_USERS},
    )
    with pytest.raises(HTTPException) as denied:
        _run(tenant_routes.get_tenant_taxonomy_route("tenant-a", principal))
    assert denied.value.status_code == 403

    monkeypatch.setattr(
        AuthUser,
        "permissions_in",
        lambda self, instance: {Permission.ADMIN},
    )
    taxonomy = _run(tenant_routes.get_tenant_taxonomy_route("tenant-a", principal))
    assert taxonomy["instance"] == "tenant-a"


def test_missing_required_body_fields_400(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")
    with pytest.raises(HTTPException) as exc:
        _run(tenant_routes.create_tenant_taxonomy_node_route("tenant-a", {"dimension": "species"}, admin))
    assert exc.value.status_code == 400


# =============================================================================
# Management API: per-tenant isolation end to end
# =============================================================================


def test_api_edit_is_tenant_isolated(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    db.create_tenant("tenant-b")
    admin_a = _tenant_admin_in("tenant-a")
    admin_b = _tenant_admin_in("tenant-b")

    _run(tenant_routes.create_tenant_taxonomy_node_route(
        "tenant-a", {"domain": "animal_husbandry", "dimension": "species", "value": "llama"}, admin_a
    ))

    tax_a = _run(tenant_routes.get_tenant_taxonomy_route("tenant-a", admin_a))
    tax_b = _run(tenant_routes.get_tenant_taxonomy_route("tenant-b", admin_b))
    assert "llama" in tax_a["domains"]["animal_husbandry"]["species"]
    assert "llama" not in tax_b["domains"]["animal_husbandry"]["species"]


def test_get_taxonomy_read_endpoint_is_tenant_scoped(db_connection):
    """The public GET /taxonomy/domain-tags resolves the caller's tenant."""
    db = db_connection
    db.create_tenant("tenant-a")
    admin_a = _tenant_admin_in("tenant-a")
    _run(tenant_routes.create_tenant_taxonomy_node_route(
        "tenant-a", {"domain": "animal_husbandry", "dimension": "species", "value": "llama"}, admin_a
    ))
    # A search-capable member of tenant-a sees its edited taxonomy.
    member_a = claims_to_user({"sub": "m", "tenant_roles": {"tenant-a": ["viewer"]}})
    taxonomy = _run(search_routes.get_domain_tag_taxonomy(member_a, instance=None))
    assert "llama" in taxonomy["domains"]["animal_husbandry"]["species"]


# =============================================================================
# Regressions: the seeded default must not resurrect itself
# =============================================================================


def test_emptying_a_taxonomy_is_not_resurrected_by_a_read(db_connection):
    """Deleting every node must STICK — a later GET must not re-seed the default.

    Row count is no longer the seeding key, so a tenant that has been emptied on
    purpose stays empty however often it is read.
    """
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")
    seeded = _run(tenant_routes.get_tenant_taxonomy_route("tenant-a", admin))
    assert seeded["domains"]  # populated from the shipped default on first touch

    # Delete every value, then every (now empty) dimension.
    for node in db.list_taxonomy_nodes("tenant-a"):
        if node["value"]:
            _run(tenant_routes.delete_tenant_taxonomy_node_route(
                "tenant-a", admin,
                domain=node["domain"], dimension=node["dimension"], value=node["value"],
            ))
    for node in db.list_taxonomy_nodes("tenant-a"):
        _run(tenant_routes.delete_tenant_taxonomy_dimension_route(
            "tenant-a", admin, domain=node["domain"], dimension=node["dimension"],
        ))
    assert db.list_taxonomy_nodes("tenant-a") == []

    # The very next read must NOT re-insert the shipped default.
    after = _run(tenant_routes.get_tenant_taxonomy_route("tenant-a", admin))
    assert after["domains"] == {}
    assert db.list_taxonomy_nodes("tenant-a") == []


def test_emptying_a_taxonomy_survives_init_db(db_connection):
    """An API restart (``init_db`` -> ``_seed_tenant_taxonomies``) must not re-seed
    an intentionally emptied tenant either."""
    db = db_connection
    db.create_tenant("tenant-a")
    db.seed_taxonomy_for_instance("tenant-a", load_taxonomy())
    for node in db.list_taxonomy_nodes("tenant-a"):
        db.delete_taxonomy_dimension("tenant-a", node["domain"], node["dimension"])
    assert db.list_taxonomy_nodes("tenant-a") == []

    db.init_db()  # simulates the process restart
    assert db.list_taxonomy_nodes("tenant-a") == []
    assert db.taxonomy_is_seeded("tenant-a") is True


def test_existing_rows_backfill_the_seed_marker(db_connection):
    """Upgrade path: a tenant seeded before the marker existed is marked, so the
    migration never re-seeds a curated taxonomy."""
    db = db_connection
    assert db.taxonomy_is_seeded("default") is True


# =============================================================================
# Regressions: deleting values must not delete the dimension
# =============================================================================


def test_deleting_last_value_keeps_the_dimension(db_connection):
    """Removing a dimension's last value leaves the dimension as an empty one."""
    db = db_connection
    db.add_taxonomy_node("tenant-a", "crop", "crop", "wheat")
    assert db.get_taxonomy("tenant-a")["domains"]["crop"]["crop"] == ["wheat"]
    assert db.delete_taxonomy_node("tenant-a", "crop", "crop", "wheat") is True
    # The dimension (and its domain) survives, empty — exactly as an explicitly
    # created empty dimension does.
    assert db.get_taxonomy("tenant-a")["domains"]["crop"]["crop"] == []


def test_delete_dimension_removes_it_entirely(db_connection):
    db = db_connection
    db.add_taxonomy_node("tenant-a", "crop", "crop", "wheat")
    db.add_taxonomy_node("tenant-a", "crop", "crop", "rice")
    assert db.delete_taxonomy_dimension("tenant-a", "crop", "crop") == 2
    assert db.get_taxonomy("tenant-a") is None
    # Unknown dimension removes nothing (route maps that to 404).
    assert db.delete_taxonomy_dimension("tenant-a", "crop", "ghost") == 0


def test_delete_dimension_route(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")
    result = _run(tenant_routes.delete_tenant_taxonomy_dimension_route(
        "tenant-a", admin, domain="animal_husbandry", dimension="species"
    ))
    assert result["deleted"] is True and result["removed"] >= 1
    taxonomy = _run(tenant_routes.get_tenant_taxonomy_route("tenant-a", admin))
    assert "species" not in taxonomy["domains"]["animal_husbandry"]
    with pytest.raises(HTTPException) as exc:
        _run(tenant_routes.delete_tenant_taxonomy_dimension_route(
            "tenant-a", admin, domain="animal_husbandry", dimension="species"
        ))
    assert exc.value.status_code == 404


def test_delete_node_without_value_is_rejected(db_connection):
    """A forgotten ``value`` must 400, not silently delete the empty-dimension
    placeholder."""
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")
    db.add_taxonomy_node("tenant-a", "crop", "crop", "")
    with pytest.raises(HTTPException) as exc:
        _run(tenant_routes.delete_tenant_taxonomy_node_route(
            "tenant-a", admin, domain="crop", dimension="crop", value=""
        ))
    assert exc.value.status_code == 400
    # The placeholder is still there.
    assert db.get_taxonomy("tenant-a")["domains"]["crop"]["crop"] == []


# =============================================================================
# Regressions: taxonomy read resolution
# =============================================================================


def test_domain_tags_read_without_any_tenant_is_403(db_connection):
    """A SEARCH-capable token with no tenant at all resolves to nothing — 403,
    never an IndexError -> 500."""
    tenantless = claims_to_user({"sub": "t", "realm_access": {"roles": ["viewer"]}})
    assert tenantless.instances == []
    with pytest.raises(HTTPException) as exc:
        _run(search_routes.get_domain_tag_taxonomy(tenantless, instance=None))
    assert exc.value.status_code == 403


def test_domain_tags_read_cross_tenant_is_404(db_connection):
    """An explicit out-of-reach ``?instance=`` is hidden as 404 and never echoes
    the tenant id back."""
    db = db_connection
    db.create_tenant("tenant-b")
    member_a = claims_to_user({"sub": "m", "tenant_roles": {"tenant-a": ["viewer"]}})
    with pytest.raises(HTTPException) as exc:
        _run(search_routes.get_domain_tag_taxonomy(member_a, instance="tenant-b"))
    assert exc.value.status_code == 404
    assert "tenant-b" not in exc.value.detail


# =============================================================================
# Regressions: the taxonomy console must be usable by a platform admin
#
# The UI has no JS test harness in this repo, so these are source-level guards on
# the two gates that made the console a dead end. They are cheap and they fail
# loudly if the old shape comes back.
# =============================================================================

TAXONOMY_VIEW = Path(__file__).resolve().parents[1] / "ui" / "src" / "views" / "TaxonomyView.jsx"


def test_console_offers_tenants_to_a_pure_platform_admin():
    """A master_admin's ``instances`` is empty (it is a member of no tenant), so
    the picker must render from one option up AND source the registry."""
    source = TAXONOMY_VIEW.read_text()
    assert "instances.length > 1" not in source
    assert "tenantOptions.length >= 1" in source
    # The tenant list comes from the registry for a platform admin.
    assert "fetchJson('/tenants')" in source
    assert "isPlatformAdmin" in source


def test_console_admin_gate_is_tenant_scoped():
    """``hasPermission('admin')`` is an ANY-tenant check — an admin in tenant A
    would get editable controls for tenant B. The gate must be per-tenant."""
    source = TAXONOMY_VIEW.read_text()
    assert "hasPermission('admin')" not in source
    assert "setCanAdmin" in source


def test_taxonomy_get_is_the_per_tenant_admin_gate(db_connection):
    """The invariant the console's scoped gate rests on: the GET itself is
    admin-in-THAT-tenant, so a caller who is admin in A and viewer in B is
    refused on B and never shown editable controls for it."""
    db = db_connection
    db.create_tenant("tenant-a")
    db.create_tenant("tenant-b")
    mixed = claims_to_user(
        {"sub": "mix", "tenant_roles": {"tenant-a": ["admin"], "tenant-b": ["viewer"]}}
    )
    assert _run(tenant_routes.get_tenant_taxonomy_route("tenant-a", mixed))["domains"]
    with pytest.raises(HTTPException) as exc:
        _run(tenant_routes.get_tenant_taxonomy_route("tenant-b", mixed))
    assert exc.value.status_code == 403


# =============================================================================
# Regression: the tagging loader must not resurrect a deleted vocabulary
# =============================================================================


def test_tagging_loader_does_not_resurrect_an_emptied_taxonomy(db_connection):
    """An admin who removed the shipped tags keeps them removed AT TAGGING TIME.

    ``load_taxonomy_for_instance`` is what the auto-tagger resolves its allowed
    vocabulary from. It used to fall back to the shipped file whenever the
    tenant had no rows, so a deliberately emptied taxonomy came back and its
    tags got applied to documents. The fallback is for tenants that were never
    seeded, so gate it on the seed marker, not on emptiness.
    """
    from pipeline.domain_tags.base import (
        DomainTag,
        validate_tags_against_taxonomy,
    )

    db = db_connection
    db.create_tenant("tenant-a")
    db.seed_taxonomy_for_instance("tenant-a", load_taxonomy())
    assert "goat" in db.get_taxonomy("tenant-a")["domains"]["animal_husbandry"]["species"]

    # The admin clears the shipped vocabulary.
    for node in db.list_taxonomy_nodes("tenant-a"):
        db.delete_taxonomy_dimension("tenant-a", node["domain"], node["dimension"])
    assert db.list_taxonomy_nodes("tenant-a") == []
    assert db.taxonomy_is_seeded("tenant-a") is True

    taxonomy = load_taxonomy_for_instance("tenant-a")
    assert taxonomy["domains"] == {}, "deleted shipped tags came back on the tagging path"

    # ...so strict validation drops a tag the admin deleted instead of applying it.
    kept = validate_tags_against_taxonomy(
        [DomainTag(dimension="species", value="goat")], taxonomy, strict=True
    )
    assert kept == []


def test_tagging_loader_still_seeds_and_falls_back_for_a_new_tenant(db_connection):
    """First-run behaviour is untouched: a tenant that was never seeded still
    resolves the shipped default, and seeding one still yields its own copy."""
    db = db_connection
    db.create_tenant("tenant-new")
    assert db.taxonomy_is_seeded("tenant-new") is False

    fresh = load_taxonomy_for_instance("tenant-new")
    assert set(fresh["domains"]["animal_husbandry"]["species"]) == {"cattle", "buffalo", "goat"}

    db.seed_taxonomy_for_instance("tenant-new", load_taxonomy())
    seeded = load_taxonomy_for_instance("tenant-new")
    assert "goat" in seeded["domains"]["animal_husbandry"]["species"]
