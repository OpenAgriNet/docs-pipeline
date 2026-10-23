"""Per-tenant tag taxonomy (Phase 5, surface P5).

DB-backed taxonomy (seed from the shipped default, node CRUD, tenant isolation),
the management API's auth discipline (404 across tenants, 403 for a weaker role,
gated on ``Permission.ADMIN`` not ``MANAGE_USERS``), and the tenant-scoped loader
that feeds domain tagging.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from pipeline.auth.jwt import claims_to_user
from pipeline.domain_tags.base import (
    DomainTag,
    load_taxonomy,
    load_taxonomy_for_instance,
    validate_tags_against_taxonomy,
)
from pipeline.routers.search import get_domain_tag_taxonomy
from pipeline.routers.tenants import (
    create_tenant_taxonomy_node_route as add_route,
    delete_tenant_taxonomy_dimension_route as delete_dimension_route,
    delete_tenant_taxonomy_node_route as delete_route,
    get_tenant_taxonomy_route as get_route,
    rename_tenant_taxonomy_node_route as rename_route,
)
from tests.helpers import master_admin, member

A, B = "tenant-a", "tenant-b"
SPECIES = ("animal_husbandry", "species")
LLAMA = {"domain": "animal_husbandry", "dimension": "species", "value": "llama"}
SHIPPED_SPECIES = {"cattle", "buffalo", "goat"}


@pytest.fixture
def db(db_connection):
    """Tenants A and B registered (taxonomy seeds on first API touch)."""
    db_connection.create_tenant(A)
    db_connection.create_tenant(B)
    return db_connection


def _status(coro) -> int:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)
    return exc.value.status_code


def _species(taxonomy) -> list:
    return taxonomy["domains"]["animal_husbandry"]["species"]


def _empty_tenant_a(db):
    """Seed tenant A, then remove every dimension the way an admin would."""
    db.seed_taxonomy_for_instance(A, load_taxonomy())
    assert "goat" in _species(db.get_taxonomy(A))
    for node in db.list_taxonomy_nodes(A):
        db.delete_taxonomy_dimension(A, node["domain"], node["dimension"])
    assert db.list_taxonomy_nodes(A) == []


# --- DB layer: seed from default + node CRUD -------------------------------------


def test_default_tenant_seeded_from_taxonomy_json(db_connection):
    taxonomy = db_connection.get_taxonomy("default")
    assert taxonomy is not None
    for domain, dims in load_taxonomy()["domains"].items():
        for dimension, values in dims.items():
            assert set(taxonomy["domains"][domain][dimension]) == {v.strip() for v in values}


def test_empty_dimension_survives_roundtrip(db_connection):
    """An empty vocabulary (``crop: []`` ships empty) is preserved, not dropped."""
    taxonomy = db_connection.get_taxonomy("default")
    assert taxonomy["domains"]["crop"]["crop"] == []
    assert "state" in taxonomy["domains"]["cross_cutting"]
    assert taxonomy["domains"]["cross_cutting"]["state"] == []


def test_seed_is_idempotent(db_connection):
    """Re-seeding a tenant that already has rows is a no-op, so curation survives."""
    db = db_connection
    before = len(db.list_taxonomy_nodes("default"))
    db.add_taxonomy_node("default", *SPECIES, "llama")
    assert db.seed_taxonomy_for_instance("default", load_taxonomy()) is False
    nodes = db.list_taxonomy_nodes("default")
    assert len(nodes) == before + 1
    assert any(n["value"] == "llama" for n in nodes)


def test_add_node_new_and_duplicate(db_connection):
    row = db_connection.add_taxonomy_node(A, *SPECIES, "yak")
    assert row is not None
    assert row["domain"] == "animal_husbandry"
    assert row["value"] == "yak"
    assert db_connection.add_taxonomy_node(A, *SPECIES, "yak") is None  # route maps to 409


def test_add_value_drops_empty_dimension_placeholder(db_connection):
    db_connection.add_taxonomy_node(A, "crop", "crop", "")
    assert db_connection.get_taxonomy(A)["domains"]["crop"]["crop"] == []
    db_connection.add_taxonomy_node(A, "crop", "crop", "wheat")
    assert db_connection.get_taxonomy(A)["domains"]["crop"]["crop"] == ["wheat"]


def test_add_node_preserves_value_casing(db_connection):
    assert db_connection.add_taxonomy_node(A, "animal_husbandry", "breed", "Zebu")["value"] == "Zebu"
    row = db_connection.add_taxonomy_node(A, "Animal_Husbandry", "Breed", "Nili")
    assert row["domain"] == "animal_husbandry"  # structural keys are lowercased
    assert row["dimension"] == "breed"


def test_rename_node(db_connection):
    db_connection.add_taxonomy_node(A, *SPECIES, "yak")
    updated = db_connection.rename_taxonomy_node(A, *SPECIES, "yak", "dzo")
    assert updated is not None
    assert updated["value"] == "dzo"
    values = {n["value"] for n in db_connection.list_taxonomy_nodes(A)}
    assert "yak" not in values
    assert "dzo" in values


def test_rename_missing_returns_none(db_connection):
    db_connection.add_taxonomy_node(A, *SPECIES, "yak")
    assert db_connection.rename_taxonomy_node(A, *SPECIES, "ghost", "x") is None


def test_rename_onto_existing_value_raises(db_connection):
    db_connection.add_taxonomy_node(A, *SPECIES, "yak")
    db_connection.add_taxonomy_node(A, *SPECIES, "ox")
    with pytest.raises(ValueError):
        db_connection.rename_taxonomy_node(A, *SPECIES, "yak", "ox")


def test_delete_node(db_connection):
    db_connection.add_taxonomy_node(A, *SPECIES, "yak")
    assert db_connection.delete_taxonomy_node(A, *SPECIES, "yak") is True
    assert db_connection.delete_taxonomy_node(A, *SPECIES, "yak") is False


def test_deleting_last_value_keeps_the_dimension(db_connection):
    """The dimension survives empty, exactly as an explicitly created empty one does."""
    db_connection.add_taxonomy_node(A, "crop", "crop", "wheat")
    assert db_connection.get_taxonomy(A)["domains"]["crop"]["crop"] == ["wheat"]
    assert db_connection.delete_taxonomy_node(A, "crop", "crop", "wheat") is True
    assert db_connection.get_taxonomy(A)["domains"]["crop"]["crop"] == []


def test_delete_dimension_removes_it_entirely(db_connection):
    db_connection.add_taxonomy_node(A, "crop", "crop", "wheat")
    db_connection.add_taxonomy_node(A, "crop", "crop", "rice")
    assert db_connection.delete_taxonomy_dimension(A, "crop", "crop") == 2
    assert db_connection.get_taxonomy(A) is None
    assert db_connection.delete_taxonomy_dimension(A, "crop", "ghost") == 0  # route maps to 404


# --- per-tenant isolation and the tagging loader ---------------------------------


def test_taxonomy_edit_is_tenant_isolated(db_connection):
    db = db_connection
    db.seed_taxonomy_for_instance(A, load_taxonomy())
    db.seed_taxonomy_for_instance(B, load_taxonomy())
    db.add_taxonomy_node(A, *SPECIES, "llama")
    db.delete_taxonomy_node(A, *SPECIES, "goat")

    a, b = _species(db.get_taxonomy(A)), _species(db.get_taxonomy(B))
    assert "llama" in a and "goat" not in a
    assert "llama" not in b and "goat" in b


def test_loader_reflects_tenant_taxonomy_for_tagging(db_connection):
    db_connection.seed_taxonomy_for_instance(A, load_taxonomy())
    db_connection.add_taxonomy_node(A, *SPECIES, "llama")
    assert "llama" in _species(load_taxonomy_for_instance(A))
    # An unseeded tenant falls back to the shipped file default.
    b = _species(load_taxonomy_for_instance(B))
    assert "llama" not in b
    assert set(b) == SHIPPED_SPECIES


def test_tagging_loader_does_not_resurrect_an_emptied_taxonomy(db):
    """Removed shipped tags stay removed AT TAGGING TIME.

    ``load_taxonomy_for_instance`` is the auto-tagger's vocabulary. It once fell
    back to the shipped file whenever a tenant had no rows, so a deliberately
    emptied taxonomy came back; the fallback is gated on the seed marker instead.
    """
    _empty_tenant_a(db)
    assert db.taxonomy_is_seeded(A) is True

    taxonomy = load_taxonomy_for_instance(A)
    assert taxonomy["domains"] == {}, "deleted shipped tags came back on the tagging path"
    assert validate_tags_against_taxonomy([DomainTag(dimension="species", value="goat")], taxonomy, strict=True) == []


def test_tagging_loader_still_seeds_and_falls_back_for_a_new_tenant(db):
    """A never-seeded tenant resolves the shipped default; seeding it yields its own copy."""
    db.create_tenant("tenant-new")
    assert db.taxonomy_is_seeded("tenant-new") is False
    assert set(_species(load_taxonomy_for_instance("tenant-new"))) == SHIPPED_SPECIES

    db.seed_taxonomy_for_instance("tenant-new", load_taxonomy())
    assert "goat" in _species(load_taxonomy_for_instance("tenant-new"))


def test_emptying_a_taxonomy_survives_init_db(db):
    """An API restart (``init_db`` -> ``_seed_tenant_taxonomies``) does not re-seed it."""
    _empty_tenant_a(db)
    db.init_db()
    assert db.list_taxonomy_nodes(A) == []
    assert db.taxonomy_is_seeded(A) is True


# --- management API: auth discipline ---------------------------------------------


@pytest.mark.parametrize("who", [master_admin(), member(A, "admin")], ids=["platform-admin", "tenant-admin"])
def test_admin_manages_the_tenant_taxonomy(db, who):
    assert asyncio.run(add_route(A, dict(LLAMA), who))["value"] == "llama"
    assert "llama" in _species(asyncio.run(get_route(A, who)))
    assert asyncio.run(rename_route(A, {**LLAMA, "new_value": "alpaca"}, who))["value"] == "alpaca"
    deleted = asyncio.run(delete_route(A, who, domain="animal_husbandry", dimension="species", value="alpaca"))
    assert deleted["deleted"] is True


@pytest.mark.parametrize(
    "who, instance, status",
    [
        (member(A, "admin"), B, 404),
        (master_admin(), "ghost-tenant", 404),
        (member(A, "viewer"), A, 403),
        (member(A, "content_curator"), A, 403),
    ],
    ids=["other-tenant-hidden-404", "unknown-tenant-404", "viewer-403", "curator-403"],
)
def test_taxonomy_management_refused(db, who, instance, status):
    """Cross-tenant or unknown is hidden as 404; a member with a weaker role gets 403."""
    assert _status(get_route(instance, who)) == status
    assert _status(add_route(instance, {**LLAMA, "value": "x"}, who)) == status


def test_taxonomy_get_is_the_per_tenant_admin_gate(db):
    """Admin in A and viewer in B: refused on B, so the console never shows B editable."""
    mixed = member({A: ["admin"], B: ["viewer"]})
    assert asyncio.run(get_route(A, mixed))["domains"]
    assert _status(get_route(B, mixed)) == 403


def test_taxonomy_auth_is_admin_not_manage_users(db, monkeypatch):
    from pipeline.auth.models import AuthUser
    from pipeline.auth.permissions import Permission

    principal = member(A, "admin")
    monkeypatch.setattr(AuthUser, "permissions_in", lambda self, instance: {Permission.MANAGE_USERS})
    assert _status(get_route(A, principal)) == 403
    monkeypatch.setattr(AuthUser, "permissions_in", lambda self, instance: {Permission.ADMIN})
    assert asyncio.run(get_route(A, principal))["instance"] == A


@pytest.mark.parametrize(
    "call, status",
    [
        (lambda who: add_route(A, dict(LLAMA), who), 409),
        (lambda who: rename_route(A, {**LLAMA, "value": "ghost", "new_value": "x"}, who), 404),
        (lambda who: delete_route(A, who, domain="animal_husbandry", dimension="species", value="ghost"), 404),
        (lambda who: add_route(A, {"dimension": "species"}, who), 400),
    ],
    ids=["duplicate-409", "rename-missing-404", "delete-missing-404", "missing-body-fields-400"],
)
def test_bad_node_request(db, call, status):
    admin = member(A, "admin")
    asyncio.run(add_route(A, dict(LLAMA), admin))
    assert _status(call(admin)) == status


def test_delete_node_without_value_is_rejected(db):
    """A forgotten ``value`` 400s instead of deleting the empty-dimension placeholder."""
    db.add_taxonomy_node(A, "crop", "crop", "")
    assert _status(delete_route(A, member(A, "admin"), domain="crop", dimension="crop", value="")) == 400
    assert db.get_taxonomy(A)["domains"]["crop"]["crop"] == []


def test_delete_dimension_route(db):
    admin = member(A, "admin")
    result = asyncio.run(delete_dimension_route(A, admin, domain="animal_husbandry", dimension="species"))
    assert result["deleted"] is True and result["removed"] >= 1
    assert "species" not in asyncio.run(get_route(A, admin))["domains"]["animal_husbandry"]
    assert _status(delete_dimension_route(A, admin, domain="animal_husbandry", dimension="species")) == 404


def test_api_edit_is_tenant_isolated(db):
    asyncio.run(add_route(A, dict(LLAMA), member(A, "admin")))
    assert "llama" in _species(asyncio.run(get_route(A, member(A, "admin"))))
    assert "llama" not in _species(asyncio.run(get_route(B, member(B, "admin"))))


def test_emptying_a_taxonomy_is_not_resurrected_by_a_read(db):
    """Deleting every node sticks: row count is not the seeding key, so a later GET
    does not re-seed the default."""
    admin = member(A, "admin")
    assert asyncio.run(get_route(A, admin))["domains"]  # seeded from the shipped default on first touch

    for node in db.list_taxonomy_nodes(A):
        if node["value"]:
            asyncio.run(delete_route(A, admin, domain=node["domain"], dimension=node["dimension"], value=node["value"]))
    for node in db.list_taxonomy_nodes(A):
        asyncio.run(delete_dimension_route(A, admin, domain=node["domain"], dimension=node["dimension"]))
    assert db.list_taxonomy_nodes(A) == []

    assert asyncio.run(get_route(A, admin))["domains"] == {}
    assert db.list_taxonomy_nodes(A) == []


# --- GET /taxonomy/domain-tags resolves the caller's tenant ----------------------


def test_get_taxonomy_read_endpoint_is_tenant_scoped(db):
    asyncio.run(add_route(A, dict(LLAMA), member(A, "admin")))
    assert "llama" in _species(asyncio.run(get_domain_tag_taxonomy(member(A, "viewer"), instance=None)))


def test_domain_tags_read_without_any_tenant_is_403(db_connection):
    """A SEARCH-capable token with no tenant resolves to nothing: 403, never IndexError -> 500."""
    tenantless = claims_to_user({"sub": "t", "realm_access": {"roles": ["viewer"]}})
    assert tenantless.instances == []
    assert _status(get_domain_tag_taxonomy(tenantless, instance=None)) == 403


def test_domain_tags_read_cross_tenant_is_404(db):
    """An out-of-reach ``?instance=`` is hidden as 404 and never echoes the tenant id."""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_domain_tag_taxonomy(member(A, "viewer"), instance=B))
    assert exc.value.status_code == 404
    assert B not in exc.value.detail
