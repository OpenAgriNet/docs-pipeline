"""Tenant index registry, resolver, and the index / tenant management routes (Phases 4+5).

The control-plane ``master_admin`` manages the tenant registry only: it is in no
tenant, so it cannot manage a tenant's indexes. ``local_bypass_user`` is the
truly data-unrestricted caller (local dev).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import pipeline.db as db_mod
import pipeline.vector_store as vector_store
from pipeline.auth.models import local_bypass_user
from pipeline.instances import default_instance
from pipeline.routers import tenants as tenant_routes
from pipeline.services import access, indexes
from tests.helpers import http_status, master_admin, member


def _create_index(instance, name, caller):
    return asyncio.run(tenant_routes.create_tenant_index(instance, {"name": name}, caller))


def _create_tenant(instance, **payload):
    return asyncio.run(tenant_routes.create_tenant_route({"instance": instance, **payload}, master_admin()))


# --- registry --------------------------------------------------------------------


def test_seed_maps_existing_index_to_default_tenant_default(db_connection):
    """After init the legacy physical index is the default tenant's default: today's single-index behaviour."""
    row = db_connection.get_default_index("default")
    assert row is not None
    assert row["marqo_index"] == "documents-index"
    assert bool(row["is_default"]) is True
    assert db_connection.resolve_marqo_index("default") == "documents-index"


def test_resolve_default_and_named(db_connection):
    db = db_connection
    db.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    db.create_index_row("tenant-a", "schemes", "t-tenant-a-schemes")
    assert db.resolve_marqo_index("tenant-a") == "t-tenant-a-vet"
    assert db.resolve_marqo_index("tenant-a", "vet") == "t-tenant-a-vet"
    assert db.resolve_marqo_index("tenant-a", "schemes") == "t-tenant-a-schemes"
    # Unknown name or tenant: None, and the caller applies the legacy fallback.
    assert db.resolve_marqo_index("tenant-a", "nope") is None
    assert db.resolve_marqo_index("ghost") is None


def test_multi_index_per_tenant_resolve_to_distinct_physical(db_connection):
    db = db_connection
    db.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    db.create_index_row("tenant-a", "schemes", "t-tenant-a-schemes")
    assert {r["marqo_index"] for r in db.list_indexes("tenant-a")} == {"t-tenant-a-vet", "t-tenant-a-schemes"}
    assert db.resolve_marqo_index("tenant-a", "vet") != db.resolve_marqo_index("tenant-a", "schemes")


def test_create_index_row_first_is_default_second_is_not(db_connection):
    db = db_connection
    assert bool(db.create_index_row("tenant-b", "a", "t-tenant-b-a", is_default=True)["is_default"]) is True
    assert bool(db.create_index_row("tenant-b", "b", "t-tenant-b-b")["is_default"]) is False
    db.create_index_row("tenant-b", "c", "t-tenant-b-c", is_default=True)
    assert [r["name"] for r in db.list_indexes("tenant-b") if r["is_default"]] == ["c"], "one default per tenant"


def test_delete_index_row(db_connection):
    db = db_connection
    db.create_index_row("tenant-c", "x", "t-tenant-c-x")
    assert db.get_index("tenant-c", "x") is not None
    assert db.delete_index_row("tenant-c", "x") is True
    assert db.get_index("tenant-c", "x") is None
    assert db.delete_index_row("tenant-c", "x") is False


def test_ensure_tenant_default_index_provisions_own_namespace(db_connection):
    """A fresh tenant's ingest lands in its OWN ``<ns><instance>-default``, never the legacy index."""
    db = db_connection
    physical = db.ensure_tenant_default_index("tenant-new")
    assert physical == "t-tenant-new-default"
    assert physical != db.default_physical_index()
    assert db.resolve_marqo_index("tenant-new") == "t-tenant-new-default"
    assert db.ensure_tenant_default_index("tenant-new") == "t-tenant-new-default"
    assert len(db.list_indexes("tenant-new")) == 1, "a second call must be idempotent"
    assert db.ensure_tenant_default_index("tenant-new", "schemes") == "t-tenant-new-schemes"
    assert bool(db.get_index("tenant-new", "schemes")["is_default"]) is False


def test_ensure_tenant_default_index_default_instance_uses_legacy(db_connection):
    assert db_connection.ensure_tenant_default_index(default_instance()) == (
        db_connection.default_physical_index()
    )


def test_resolve_ingest_index_name_honors_same_tenant_request(db_connection):
    db = db_connection
    db.create_index_row("tenant-a", "default", "t-tenant-a-default", is_default=True)
    db.create_index_row("tenant-a", "schemes", "t-tenant-a-schemes")
    db.create_index_row("tenant-b", "default", "t-tenant-b-default", is_default=True)
    # A same-tenant secondary index (Indexes card reindex) wins over the logical default.
    assert db.resolve_ingest_index_name("tenant-a", "default", requested_index_name="t-tenant-a-schemes") == (
        "t-tenant-a-schemes"
    )
    # Another tenant's physical name is ignored in favour of the registry.
    assert db.resolve_ingest_index_name("tenant-a", "default", requested_index_name="t-tenant-b-default") == (
        "t-tenant-a-default"
    )
    assert db.resolve_ingest_index_name("tenant-a", "default") == "t-tenant-a-default"


def test_reverse_lookup_index_to_tenant(db_connection):
    db_connection.create_index_row("tenant-a", "vet", "t-tenant-a-vet")
    assert db_connection.get_index_by_marqo_index("t-tenant-a-vet")["instance"] == "tenant-a"
    assert db_connection.get_index_by_marqo_index("does-not-exist") is None


# --- resolve_index / assert_index_access -----------------------------------------


def test_api_resolve_index_registry_and_no_cross_tenant_fallback(db_connection):
    """A non-default tenant without an index resolves to None, NEVER the default tenant's
    physical index (that leaked the default tenant's documents on a read)."""
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    assert indexes.resolve_index("tenant-a", "vet") == "t-tenant-a-vet"
    assert indexes.resolve_index("tenant-a") == "t-tenant-a-vet"
    assert indexes.resolve_index("ghost") is None
    db_mod.create_tenant_row("tenant-x", display_name="Tenant X")
    assert indexes.resolve_index("tenant-x") is None
    assert http_status(lambda: indexes.resolve_index("tenant-a", "missing")) == 404


def test_api_resolve_index_default_instance_legacy_backcompat(db_connection):
    """The default instance alone maps a name=None miss to the legacy physical index,
    even with its seeded registry row dropped."""
    default = default_instance()
    assert indexes.resolve_index(default) == vector_store.default_physical_index()
    db_mod.delete_index_row(default, "default")
    assert db_mod.get_default_index(default) is None
    assert indexes.resolve_index(default) == vector_store.default_physical_index()


def test_assert_index_access_denies_cross_tenant(db_connection):
    db_mod.create_index_row("tenant-b", "vet", "t-tenant-b-vet", is_default=True)
    viewer_a = member("tenant-a", "viewer")
    assert http_status(lambda: access.assert_index_access(viewer_a, "tenant-b", "vet")) == 404
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    assert access.assert_index_access(viewer_a, "tenant-a", "vet") == "t-tenant-a-vet"
    # Data-unrestricted passes anywhere; the control-plane admin is in no tenant.
    assert access.assert_index_access(local_bypass_user(), "tenant-b", "vet") == "t-tenant-b-vet"
    assert http_status(lambda: access.assert_index_access(master_admin(), "tenant-b", "vet")) == 404


def test_assert_index_access_named_unregistered_is_404(db_connection):
    assert http_status(lambda: access.assert_index_access(local_bypass_user(), "default", "nope")) == 404


def test_assert_marqo_index_access_restricted_cannot_target_unregistered(db_connection):
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet")
    curator = member("tenant-a", "content_curator")
    assert access.assert_marqo_index_access(curator, "t-tenant-a-vet") == "t-tenant-a-vet"
    assert http_status(lambda: access.assert_marqo_index_access(curator, "t-tenant-b-vet")) == 404
    # Only a data-unrestricted caller may target an unregistered legacy index.
    assert access.assert_marqo_index_access(local_bypass_user(), "legacy-index") == "legacy-index"
    assert http_status(lambda: access.assert_marqo_index_access(master_admin(), "legacy-index")) == 404


# --- index management ------------------------------------------------------------


def test_create_index_gating_curator_in_tenant_allowed(db_connection, fake_marqo):
    """A content_curator (``pipeline``) in the tenant may self-serve indexes."""
    curator = member("tenant-a", "content_curator")
    first = _create_index("tenant-a", "vet", curator)
    assert first["marqo_index"] == "t-tenant-a-vet"
    assert first["is_default"] is True
    second = _create_index("tenant-a", "schemes", curator)
    assert second["marqo_index"] == "t-tenant-a-schemes"
    assert second["is_default"] is False
    assert first["marqo_index"] != second["marqo_index"]
    assert fake_marqo.created_indexes == ["t-tenant-a-vet", "t-tenant-a-schemes"]


def test_create_index_gating_viewer_denied(db_connection, fake_marqo):
    call = tenant_routes.create_tenant_index("tenant-a", {"name": "vet"}, member("tenant-a", "viewer"))
    assert http_status(call) == 403


def test_create_index_duplicate_conflicts(db_connection, fake_marqo):
    curator = member("tenant-a", "content_curator")
    _create_index("tenant-a", "vet", curator)
    assert http_status(tenant_routes.create_tenant_index("tenant-a", {"name": "vet"}, curator)) == 409


def test_list_indexes_gated_to_tenant(db_connection, fake_marqo):
    _create_index("tenant-a", "vet", member("tenant-a", "content_curator"))
    rows = asyncio.run(tenant_routes.list_tenant_indexes("tenant-a", member("tenant-a", "viewer")))
    assert {r["name"] for r in rows} == {"vet"}, "any access to the tenant may list"
    assert http_status(tenant_routes.list_tenant_indexes("tenant-a", member("tenant-b", "viewer"))) == 404


def test_delete_index_requires_admin_and_guards(db_connection, fake_marqo):
    """Index management is data plane: a tenant admin, not the control-plane master_admin."""
    admin = member("tenant-a", "admin")
    _create_index("tenant-a", "vet", admin)  # the default
    _create_index("tenant-a", "schemes", admin)
    curator = member("tenant-a", "content_curator")
    assert http_status(tenant_routes.delete_tenant_index("tenant-a", "schemes", curator, force=False)) == 403
    assert http_status(tenant_routes.delete_tenant_index("tenant-a", "vet", admin, force=False)) == 409, (
        "the tenant default went while another index exists"
    )
    out = asyncio.run(tenant_routes.delete_tenant_index("tenant-a", "schemes", admin, force=False))
    assert out["name"] == "schemes"
    assert out["marqo_dropped"] is True
    assert fake_marqo.deleted_indexes == ["t-tenant-a-schemes"]
    assert db_mod.get_index("tenant-a", "schemes") is None


def test_delete_index_with_documents_requires_force_and_reassigns(db_connection, fake_marqo):
    admin = member("tenant-a", "admin")
    _create_index("tenant-a", "vet", admin)
    _create_index("tenant-a", "schemes", admin)
    db_mod.upsert_document(
        workflow_id="wf-s", document_id="d-s", filename="s.pdf", filepath="/tmp/s.pdf",
        instance="tenant-a", index="schemes",
    )
    assert http_status(tenant_routes.delete_tenant_index("tenant-a", "schemes", admin, force=False)) == 409
    out = asyncio.run(tenant_routes.delete_tenant_index("tenant-a", "schemes", admin, force=True))
    assert out["documents_reassigned"] == 1
    assert db_mod.get_document("wf-s")["index"] is None, "documents go back to the tenant default"


# --- H2: physical-name collisions and cross-tenant destruction -------------------


def test_new_marqo_index_name_rejects_dash_in_name():
    """A '-' in the logical name could alias another (instance, name) through the '-' join."""
    assert http_status(lambda: indexes.new_marqo_index_name("tenant-a", "foo-bar")) == 400
    assert indexes.new_marqo_index_name("tenant-a", "vet_2024") == "t-tenant-a-vet_2024"


def test_create_index_name_with_dash_rejected(db_connection, fake_marqo):
    call = tenant_routes.create_tenant_index("tenant-a", {"name": "vet-schemes"}, member("tenant-a", "content_curator"))
    assert http_status(call) == 400


def test_create_tenant_physical_index_collision_409(db_connection, fake_marqo):
    """409, not adoption, when another tenant owns the default's physical name, and no orphan tenant row."""
    db_mod.create_index_row(instance="other", name="default", marqo_index="t-tenant-x-default", is_default=True)
    assert http_status(tenant_routes.create_tenant_route({"instance": "tenant-x"}, master_admin())) == 409
    assert db_mod.get_tenant("tenant-x") is None


def test_create_marqo_index_refuses_to_adopt_foreign_physical(db_connection, fake_marqo):
    """A physical index that exists in Marqo but is not this tenant's registered one is 409
    (the fake raises on a second create, so adopting it would fail too)."""
    fake_marqo.add_index("t-tenant-a-vet")
    assert http_status(lambda: indexes.create_marqo_index_with_schema("t-tenant-a-vet")) == 409
    # Once registered, re-creating it is an idempotent no-op that returns the settings.
    db_mod.create_index_row(instance="tenant-a", name="vet", marqo_index="t-tenant-a-vet")
    assert isinstance(indexes.create_marqo_index_with_schema("t-tenant-a-vet"), dict)
    assert fake_marqo.created_indexes == []


# --- tenant management -----------------------------------------------------------


def test_create_tenant_provisions_default_index(db_connection, fake_marqo):
    out = _create_tenant("tenant-x", display_name="Tenant X")
    assert out["tenant"]["id"] == "tenant-x"
    assert out["default_index"]["marqo_index"] == "t-tenant-x-default"
    assert out["default_index"]["is_default"] is True
    assert db_mod.resolve_marqo_index("tenant-x") == "t-tenant-x-default"


def test_create_tenant_duplicate_adopts(db_connection, fake_marqo):
    """Re-creating an existing instance ADOPTS it rather than 409-ing, with no second default index."""
    assert _create_tenant("tenant-x")["adopted"] is False
    second = _create_tenant("tenant-x")
    assert second["adopted"] is True
    assert second["default_index"]["marqo_index"] == "t-tenant-x-default"
    assert fake_marqo.created_indexes == ["t-tenant-x-default"]
    assert len(db_mod.list_indexes("tenant-x")) == 1


def test_suspend_and_delete_tenant(db_connection, fake_marqo):
    _create_tenant("tenant-x")
    _create_index("tenant-x", "extra", member("tenant-x", "admin"))
    assert asyncio.run(tenant_routes.suspend_tenant_route("tenant-x", master_admin()))["status"] == "suspended"
    assert http_status(tenant_routes.delete_tenant_route("tenant-x", master_admin(), confirm=False)) == 400
    out = asyncio.run(tenant_routes.delete_tenant_route("tenant-x", master_admin(), confirm=True))
    assert out["registry_rows_removed"] == 2
    assert sorted(fake_marqo.deleted_indexes) == ["t-tenant-x-default", "t-tenant-x-extra"]
    assert [d["error"] for d in out["indexes_dropped"]] == [None, None]
    assert db_mod.get_tenant("tenant-x") is None
    assert db_mod.list_indexes("tenant-x") == []


# Qdrant backend mapping and tenant isolation.

def test_qdrant_cutover_unrestricted_search_uses_qdrant_index_name(db_connection, monkeypatch):
    """Kanav's reproduction: clean DB seeds search_index_name=documents-index
    even when QDRANT_INDEX_NAME is set. Unrestricted search must still land on
    the Qdrant collection after the backend flip."""
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_INDEX_NAME", "shadow-qdrant")
    monkeypatch.setenv("MARQO_INDEX_NAME", "documents-index")
    monkeypatch.delenv("QDRANT_INDEX_MAP", raising=False)
    monkeypatch.delenv("QDRANT_INDEX_SUFFIX", raising=False)
    assert db_mod.get_search_settings()["indexName"] == "shadow-qdrant"
    assert indexes.resolve_unrestricted_search_index() == "shadow-qdrant"
    default = default_instance()
    assert indexes.resolve_index(default) == "shadow-qdrant"
    assert access.assert_index_access(local_bypass_user(), default) == "shadow-qdrant"



def test_qdrant_cutover_tenant_scoped_search_maps_registry_names(db_connection, monkeypatch):
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_INDEX_NAME", "shadow-qdrant")
    monkeypatch.setenv("QDRANT_INDEX_MAP", "t-tenant-a-vet=t-tenant-a-vet-qdrant")
    monkeypatch.delenv("QDRANT_INDEX_SUFFIX", raising=False)
    assert indexes.resolve_index("tenant-a", "vet") == "t-tenant-a-vet-qdrant"
    assert access.assert_index_access(member("tenant-a", "viewer"), "tenant-a", "vet") == "t-tenant-a-vet-qdrant"
    monkeypatch.delenv("QDRANT_INDEX_MAP", raising=False)
    monkeypatch.setenv("QDRANT_INDEX_SUFFIX", "-qdrant")
    assert indexes.resolve_index("tenant-a") == "t-tenant-a-vet-qdrant"
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "marqo")
    assert indexes.resolve_index("tenant-a", "vet") == "t-tenant-a-vet"



def test_qdrant_cutover_infers_tenant_suffix_from_env_names(db_connection, monkeypatch):
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("MARQO_INDEX_NAME", "documents-index")
    monkeypatch.setenv("QDRANT_INDEX_NAME", "documents-index-qdrant")
    monkeypatch.delenv("QDRANT_INDEX_MAP", raising=False)
    monkeypatch.delenv("QDRANT_INDEX_SUFFIX", raising=False)
    assert indexes.resolve_index("tenant-a", "vet") == "t-tenant-a-vet-qdrant"



def test_qdrant_cutover_put_settings_does_not_persist_resolved_name(db_connection, monkeypatch):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_INDEX_NAME", "shadow-qdrant")
    monkeypatch.setenv("MARQO_INDEX_NAME", "documents-index")
    db_mod.update_search_settings({"indexName": "shadow-qdrant"})
    raw = db_mod.get_all_settings()["search_index_name"]["value"]
    assert raw == "documents-index"
    assert db_mod.get_search_settings()["indexName"] == "shadow-qdrant"



def test_qdrant_cutover_purge_translates_stored_marqo_names(db_connection, monkeypatch):
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    db_mod.upsert_document_index_status(
        "wf-cutover-purge", "t-tenant-a-vet", status="indexed", chunk_count_indexed=1
    )
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_INDEX_NAME", "documents-index-qdrant")
    monkeypatch.setenv("MARQO_INDEX_NAME", "documents-index")
    monkeypatch.delenv("QDRANT_INDEX_MAP", raising=False)
    monkeypatch.delenv("QDRANT_INDEX_SUFFIX", raising=False)
    calls = []

    def _fake_delete(doc_id, index_name="documents-index", workflow_id=None):
        calls.append(index_name)
        return {"deleted": 1, "index_name": index_name}

    monkeypatch.setattr(indexes, "delete_chunks_from_marqo", _fake_delete)
    result = indexes.purge_document_search_indexes(
        workflow_id="wf-cutover-purge",
        document_id="doc-cutover-purge",
        instance="tenant-a",
        logical_index="vet",
    )
    assert "t-tenant-a-vet-qdrant" in calls
    assert "t-tenant-a-vet" not in calls
    assert "t-tenant-a-vet-qdrant" in result["indexes"]



def test_qdrant_registry_seed_keeps_names_for_rollback(db_connection, monkeypatch):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("MARQO_INDEX_NAME", "documents-index")
    monkeypatch.setenv("QDRANT_INDEX_NAME", "documents-index-qdrant")
    with db_mod.get_connection() as conn:
        conn.execute("DELETE FROM tenant_indexes")
        conn.commit()
    db_mod.seed_tenant_indexes()
    inst = default_instance()
    assert db_mod.resolve_marqo_index(inst) == "documents-index"
    assert indexes.resolve_index(inst) == "documents-index-qdrant"
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "marqo")
    assert indexes.resolve_index(inst) == "documents-index"



def test_qdrant_resolved_name_access_and_settings_preserve_owner(db_connection, monkeypatch):
    db_mod.create_index_row("tenant-a", "vet", "t-tenant-a-vet", is_default=True)
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_INDEX_MAP", "t-tenant-a-vet=vet-shadow")
    assert access.assert_marqo_index_access(member("tenant-a", "viewer"), "vet-shadow") == "vet-shadow"
    with pytest.raises(HTTPException) as exc:
        access.assert_marqo_index_access(member("tenant-b", "viewer"), "vet-shadow")
    assert exc.value.status_code == 404
    db_mod.update_search_settings({"indexName": "vet-shadow"})
    assert db_mod.get_setting("search_index_name")["value"] == "t-tenant-a-vet"
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "marqo")
    assert db_mod.get_search_settings()["indexName"] == "t-tenant-a-vet"



def test_qdrant_provision_and_delete_use_mapped_collection(db_connection, monkeypatch):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "qdrant")
    monkeypatch.setenv("QDRANT_INDEX_MAP", "t-tenant-a-vet=vet-shadow")
    store = MagicMock()
    store.index_exists.return_value = False
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: store)
    asyncio.run(tenant_routes.create_tenant_index("tenant-a", {"name": "vet"}, member("tenant-a", "admin")))
    assert db_mod.get_index("tenant-a", "vet")["marqo_index"] == "t-tenant-a-vet"
    assert store.create_index.call_args.args[0] == "vet-shadow"
    asyncio.run(tenant_routes.delete_tenant_index("tenant-a", "vet", member("tenant-a", "admin"), force=True))
    store.delete_index.assert_called_once_with("vet-shadow")
