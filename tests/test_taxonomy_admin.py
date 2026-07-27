"""Tests for per-tenant tag taxonomy management (Phase 5, surface P5).

Covers the DB-backed per-tenant taxonomy (seed-from-default, node CRUD, tenant
isolation), the management API's auth discipline (403/404 mirroring
member-management), and the tenant-scoped loader that feeds domain tagging.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import pipeline.api as api
from pipeline.auth.jwt import claims_to_user
from pipeline.domain_tags.base import load_taxonomy, load_taxonomy_for_instance


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
# Management API: auth discipline (mirrors member management)
# =============================================================================


def test_platform_admin_manages_any_tenant(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _platform_admin()

    created = _run(api.create_tenant_taxonomy_node_route(
        "tenant-a", {"domain": "animal_husbandry", "dimension": "species", "value": "llama"}, admin
    ))
    assert created["value"] == "llama"

    taxonomy = _run(api.get_tenant_taxonomy_route("tenant-a", admin))
    assert "llama" in taxonomy["domains"]["animal_husbandry"]["species"]


def test_tenant_admin_manages_own_taxonomy(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")

    created = _run(api.create_tenant_taxonomy_node_route(
        "tenant-a", {"domain": "animal_husbandry", "dimension": "species", "value": "llama"}, admin
    ))
    assert created["value"] == "llama"

    renamed = _run(api.rename_tenant_taxonomy_node_route(
        "tenant-a",
        {"domain": "animal_husbandry", "dimension": "species", "value": "llama", "new_value": "alpaca"},
        admin,
    ))
    assert renamed["value"] == "alpaca"

    deleted = _run(api.delete_tenant_taxonomy_node_route(
        "tenant-a", admin, domain="animal_husbandry", dimension="species", value="alpaca"
    ))
    assert deleted["deleted"] is True


def test_create_duplicate_node_409(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")
    body = {"domain": "animal_husbandry", "dimension": "species", "value": "llama"}
    _run(api.create_tenant_taxonomy_node_route("tenant-a", body, admin))
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_taxonomy_node_route("tenant-a", body, admin))
    assert exc.value.status_code == 409


def test_rename_missing_node_404(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")
    with pytest.raises(HTTPException) as exc:
        _run(api.rename_tenant_taxonomy_node_route(
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
        _run(api.delete_tenant_taxonomy_node_route(
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
        _run(api.get_tenant_taxonomy_route("tenant-b", admin_a))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc2:
        _run(api.create_tenant_taxonomy_node_route(
            "tenant-b", {"domain": "animal_husbandry", "dimension": "species", "value": "x"}, admin_a
        ))
    assert exc2.value.status_code == 404


def test_unknown_tenant_404(db_connection):
    admin = _platform_admin()
    with pytest.raises(HTTPException) as exc:
        _run(api.get_tenant_taxonomy_route("ghost-tenant", admin))
    assert exc.value.status_code == 404


def test_viewer_and_curator_cannot_manage_taxonomy_403(db_connection):
    """A member with an insufficient role gets 403 (not 404)."""
    db = db_connection
    db.create_tenant("tenant-a")
    for principal in (_viewer_in("tenant-a"), _curator_in("tenant-a")):
        with pytest.raises(HTTPException) as exc:
            _run(api.get_tenant_taxonomy_route("tenant-a", principal))
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc2:
            _run(api.create_tenant_taxonomy_node_route(
                "tenant-a", {"domain": "animal_husbandry", "dimension": "species", "value": "x"}, principal
            ))
        assert exc2.value.status_code == 403


def test_missing_required_body_fields_400(db_connection):
    db = db_connection
    db.create_tenant("tenant-a")
    admin = _tenant_admin_in("tenant-a")
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_taxonomy_node_route("tenant-a", {"dimension": "species"}, admin))
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

    _run(api.create_tenant_taxonomy_node_route(
        "tenant-a", {"domain": "animal_husbandry", "dimension": "species", "value": "llama"}, admin_a
    ))

    tax_a = _run(api.get_tenant_taxonomy_route("tenant-a", admin_a))
    tax_b = _run(api.get_tenant_taxonomy_route("tenant-b", admin_b))
    assert "llama" in tax_a["domains"]["animal_husbandry"]["species"]
    assert "llama" not in tax_b["domains"]["animal_husbandry"]["species"]


def test_get_taxonomy_read_endpoint_is_tenant_scoped(db_connection):
    """The public GET /taxonomy/domain-tags resolves the caller's tenant."""
    db = db_connection
    db.create_tenant("tenant-a")
    admin_a = _tenant_admin_in("tenant-a")
    _run(api.create_tenant_taxonomy_node_route(
        "tenant-a", {"domain": "animal_husbandry", "dimension": "species", "value": "llama"}, admin_a
    ))
    # A search-capable member of tenant-a sees its edited taxonomy.
    member_a = claims_to_user({"sub": "m", "tenant_roles": {"tenant-a": ["viewer"]}})
    taxonomy = _run(api.get_domain_tag_taxonomy(member_a, instance=None))
    assert "llama" in taxonomy["domains"]["animal_husbandry"]["species"]
