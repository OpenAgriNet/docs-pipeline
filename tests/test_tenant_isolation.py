"""Cross-tenant isolation harness: the Phase 6 definition-of-done guardrail.

The invariant (TENANT_ISOLATION_PLAN.md §6): a tenant-A token gets 404/empty on
every tenant-B doc, chunk, artifact URL, run and search. Existence is never
leaked, so cross-tenant access is **404** (not 403); a reachable tenant with an
insufficient role is **403**. The realm ``master_admin`` manages the tenant
registry only and reaches no tenant's data.

Route handlers are called directly (the app lifespan needs Temporal), so every
``Query()`` / ``Header()`` parameter is passed explicitly and dependency-enforced
gates are exercised by calling the dependency.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import pipeline.db as db_mod
import pipeline.vector_store as vector_store
from pipeline.auth.deps import require_platform_admin
from pipeline.auth.models import local_bypass_user
from pipeline.models import ChunkUpdate, DocumentCohortsResponse, PageUpdate
from pipeline.routers import (
    admin as admin_routes,
    content as content_routes,
    documents as document_routes,
    documents_actions as action_routes,
    search as search_routes,
    tenants as tenant_routes,
)
from pipeline.services import access, indexes
from tests.helpers import PASSAGE_FIELDS, FakeIndex, http_status, master_admin, member, seed_document

A = "tenant-a"
B = "tenant-b"
WF_A = "wf-a"
WF_B = "wf-b"
PAGE = {"limit": 100, "offset": 0}
NO_HEADERS = {"x_include_demo": None, "x_include_disabled": None}
#: The legacy single-tenant index: no filterable ``instance`` field.
LEGACY_FIELDS = PASSAGE_FIELDS - {"instance"}


@pytest.fixture
def seeded(db_connection):
    """Two tenants, each with one document (page, chunk, audit row, artifact, run)
    and a registered default index."""
    for wf, doc_id, inst in ((WF_A, "d-a", A), (WF_B, "d-b", B)):
        seed_document(
            db_connection, wf, document_id=doc_id, filename=f"{inst}-secret.pdf",
            stage="ocr_review", instance=inst, pages=[f"{inst} page"], chunks=[f"{inst} chunk body"],
        )
        db_connection.log_audit(
            workflow_id=wf, document_id=doc_id, action_type="approval",
            field_name="ocr_approved", new_value="True",
        )
        db_connection.create_index_row(inst, "vet", f"t-{inst}-vet", is_default=True)
    return {
        "art_a": db_connection.add_document_artifact(WF_A, "original_upload", "/tmp/a.bin", filename="a.bin"),
        "art_b": db_connection.add_document_artifact(WF_B, "original_upload", "/tmp/b.bin", filename="b.bin"),
        "run_a": db_connection.create_document_job(workflow_id=WF_A, job_type="pipeline"),
        "run_b": db_connection.create_document_job(workflow_id=WF_B, job_type="pipeline"),
    }


def _hit(_id, doc_id, instance, text):
    return {"_id": _id, "doc_id": doc_id, "instance": instance, "text": text, "is_reference": False}


def _legacy_default_index(fake_marqo, *hits) -> str:
    """Seed the default tenant's legacy physical index; returns its name."""
    name = vector_store.default_physical_index()
    fake_marqo.add_index(name, hits, fields=LEGACY_FIELDS)
    return name


def _control_probe_lands_on_the_fake(fake_marqo) -> str:
    """Positive counterweight for the ``fake_marqo.searches == []`` leak asserts.

    Those are PURE NEGATIVES, and an empty recorder also describes a fake that
    never bound. An UNRESTRICTED caller falls through to the configured default
    index, so its search MUST land on the fake: that proves both that client
    construction goes through the fake and that a search would be recorded.
    Clears the recorders afterwards so the probe under test starts clean.
    """
    fake_marqo.reset()
    probe = asyncio.run(search_routes.run_marqo_search({"query": "milk"}, local_bypass_user()))
    target = probe["effective_config"]["index_name"]
    assert target, "control probe resolved no index: fixture setup is wrong"
    fake_marqo.assert_constructed("the search under test could not have been recorded either.")
    assert [name for name, _ in fake_marqo.searches] == [target], (
        f"the control search did not land on the fake index, so `searches == []` proves nothing. {fake_marqo.searches}"
    )
    fake_marqo.reset()
    return target


# --- documents (list / summary / detail) -----------------------------------------


def test_documents_list_excludes_other_tenant(seeded):
    payload = asyncio.run(document_routes.list_documents(member(A, "content_curator"), stage=None, **PAGE, **NO_HEADERS))
    wids = {r.workflow_id for r in payload.items}
    assert wids == {WF_A}
    assert WF_B not in wids
    assert payload.total == 1
    assert payload.limit == 100
    assert payload.offset == 0


def test_documents_summary_excludes_other_tenant(seeded):
    summary = asyncio.run(document_routes.get_documents_summary(member(A, "content_curator"), **NO_HEADERS))
    assert summary["total_documents"] == 1


def test_get_document_cross_tenant_is_404(seeded):
    assert http_status(document_routes.get_document(WF_B, member(A, "viewer"))) == 404  # not 403


def test_get_own_document_succeeds(seeded):
    assert asyncio.run(document_routes.get_document(WF_A, member(A, "viewer"))).workflow_id == WF_A


def test_platform_admin_sees_no_tenant_data(seeded):
    """The control-plane admin has NO data access: the document plane is empty and
    any specific tenant document is hidden (404), same as a non-member."""
    payload = asyncio.run(document_routes.list_documents(master_admin(), stage=None, **PAGE, **NO_HEADERS))
    assert payload.items == []
    assert payload.total == 0
    assert http_status(document_routes.get_document(WF_B, master_admin())) == 404
    assert http_status(document_routes.get_document(WF_A, master_admin())) == 404


# --- doc sub-resources (pages / chunks / artifacts / pdf / export / audit) -------


def test_pages_cross_tenant_is_404(seeded):
    viewer = member(A, "viewer")
    assert http_status(content_routes.list_pages(WF_B, viewer)) == 404
    assert http_status(content_routes.get_page(WF_B, viewer, page_num=1)) == 404


def test_chunks_cross_tenant_is_404(seeded):
    viewer = member(A, "viewer")
    assert http_status(content_routes.list_chunks(WF_B, viewer, include_excluded=False)) == 404
    assert http_status(content_routes.get_chunk(WF_B, viewer, chunk_num=1)) == 404


def test_artifacts_cross_tenant_is_404(seeded):
    viewer, art_b = member(A, "viewer"), seeded["art_b"]
    assert http_status(document_routes.list_document_artifacts(WF_B, viewer)) == 404
    assert http_status(document_routes.get_document_artifact(WF_B, viewer, art_b)) == 404
    assert http_status(document_routes.get_document_artifact_content(WF_B, viewer, art_b)) == 404


def test_pdf_cross_tenant_is_404(seeded):
    assert http_status(document_routes.get_document_pdf(WF_B, member(A, "viewer"))) == 404


def test_export_cross_tenant_is_404(seeded):
    viewer = member(A, "viewer")
    assert http_status(content_routes.export_markdown(WF_B, viewer)) == 404
    assert http_status(content_routes.export_chunks(WF_B, viewer, include_excluded=False)) == 404


def test_per_document_audit_cross_tenant_is_404(seeded):
    call = document_routes.get_document_audit_log(WF_B, member(A, "viewer"), action_type=None, limit=50, offset=0)
    assert http_status(call) == 404


def test_runtime_cross_tenant_is_404(seeded):
    """Tenancy 404 happens before Temporal describe / progress assembly."""
    assert http_status(document_routes.get_document_runtime(WF_B, member(A, "viewer"))) == 404


def test_global_audit_list_excludes_other_tenant(seeded):
    """Global ``/audit`` must be instance-scoped (it once was not)."""
    resp = asyncio.run(admin_routes.get_all_audit_logs(member(A, "viewer"), action_type=None, limit=50, offset=0))
    wids = {entry.workflow_id for entry in resp.logs}
    assert wids == {WF_A}
    assert WF_B not in wids
    assert resp.total == 1
    # Control-plane admin has no data scope: the global audit trail is empty.
    admin_resp = asyncio.run(admin_routes.get_all_audit_logs(master_admin(), action_type=None, limit=50, offset=0))
    assert admin_resp.logs == []
    assert admin_resp.total == 0


def test_provenance_chunk_cross_tenant_is_404(seeded):
    call = content_routes.resolve_provenance_chunk(
        MagicMock(), member(A, "viewer"), doc_id=WF_B, chunk_num=1, marqo_id=None, index_name="documents-index",
    )
    assert http_status(call) == 404


def test_own_doc_subresources_and_provenance_ok(seeded):
    """Positive control: the tenant-a caller reaches its own sub-resources."""
    viewer = member(A, "viewer")
    assert len(asyncio.run(content_routes.list_pages(WF_A, viewer))) == 1
    assert len(asyncio.run(content_routes.list_chunks(WF_A, viewer, include_excluded=False))) == 1
    assert {a["id"] for a in asyncio.run(document_routes.list_document_artifacts(WF_A, viewer))} == {seeded["art_a"]}
    assert "content" in asyncio.run(content_routes.export_markdown(WF_A, viewer))
    prov = asyncio.run(content_routes.resolve_provenance_chunk(
        MagicMock(), viewer, doc_id=WF_A, chunk_num=1, marqo_id=None, index_name="documents-index",
    ))
    assert prov["workflow_id"] == WF_A


# --- doc-scoped Marqo status (/documents/{}/marqo*) ------------------------------


def test_document_marqo_status_cross_tenant_is_404(seeded):
    viewer = member(A, "viewer")
    assert http_status(content_routes.get_document_marqo_status(WF_B, viewer, index_name="documents-index")) == 404
    assert http_status(content_routes.list_document_marqo_chunks(WF_B, viewer, index_name="documents-index")) == 404


def test_document_marqo_targeting_other_tenant_index_is_404(seeded):
    """Own doc, but a tenant-B physical index name: hidden as 404."""
    call = content_routes.get_document_marqo_status(WF_A, member(A, "content_curator"), index_name="t-tenant-b-vet")
    assert http_status(call) == 404


# --- mutations (approve / retry / reingest / patch) ------------------------------


def test_mutation_cross_tenant_is_404(seeded):
    curator = member(A, "content_curator")
    assert http_status(action_routes.approve_ocr(WF_B, curator)) == 404
    assert http_status(action_routes.reingest_document(WF_B, curator, marqo_url="", index_name="documents-index")) == 404
    assert http_status(action_routes.retry_ocr(WF_B, curator)) == 404
    assert http_status(content_routes.update_page(WF_B, PageUpdate(), curator, page_num=1)) == 404
    assert http_status(content_routes.update_chunk(WF_B, ChunkUpdate(), curator, chunk_num=1)) == 404


def test_mutation_wrong_role_in_own_tenant_is_403(seeded):
    """A viewer in tenant-a may READ tenant-a but not mutate it: 403, not 404."""
    viewer = member(A, "viewer")
    assert http_status(action_routes.approve_ocr(WF_A, viewer)) == 403
    assert http_status(action_routes.reingest_document(WF_A, viewer, marqo_url="", index_name="documents-index")) == 403
    assert http_status(content_routes.update_page(WF_A, PageUpdate(), viewer, page_num=1)) == 403


# --- runs (/runs, /operations/queue, /runs/{id}) ---------------------------------


def test_runs_list_excludes_other_tenant(seeded):
    runs = asyncio.run(admin_routes.list_runs(member(A, "content_curator"), **PAGE, status=None))
    assert {r["workflow_id"] for r in runs} == {WF_A}


def test_operations_queue_excludes_other_tenant(seeded):
    resp = asyncio.run(admin_routes.get_operations_queue(member(A, "content_curator"), **PAGE, **NO_HEADERS))
    assert {item.workflow_id for item in resp.items} == {WF_A}
    assert resp.total == 1


def test_get_run_cross_tenant_is_404(seeded):
    assert http_status(admin_routes.get_run(seeded["run_b"], member(A, "content_curator"))) == 404


def test_own_run_accessible_and_platform_admin_sees_none(seeded):
    assert asyncio.run(admin_routes.get_run(seeded["run_a"], member(A, "content_curator")))["workflow_id"] == WF_A
    # Control-plane admin has no data scope: no runs, and a specific run is hidden (404).
    assert asyncio.run(admin_routes.list_runs(master_admin(), **PAGE, status=None)) == []
    assert http_status(admin_routes.get_run(seeded["run_a"], master_admin())) == 404


# --- search (/marqo/search) ------------------------------------------------------


def test_search_resolves_to_own_index_and_filters_out_other_tenant(seeded, fake_marqo):
    # tenant-a's physical index accidentally holds a tenant-b chunk; the per-chunk
    # instance filter must still strip it.
    fake_marqo.add_index("t-tenant-a-vet", [_hit("1", "d-a", A, "a"), _hit("2", "d-b", B, "b-leak")])
    result = asyncio.run(search_routes.run_marqo_search({"query": "milk"}, member(A, "content_curator")))
    assert result["effective_config"]["index_name"] == "t-tenant-a-vet"
    assert "instance:(tenant-a)" in (result["effective_config"]["filter_string"] or "")
    assert {h["instance"] for h in result["hits"]} == {A}


def test_search_targeting_other_tenant_physical_index_is_404(seeded, fake_marqo):
    body = {"query": "x", "index_name": "t-tenant-b-vet"}
    assert http_status(search_routes.run_marqo_search(body, member(A, "content_curator"))) == 404


def test_search_targeting_other_tenant_instance_is_404(seeded, fake_marqo):
    body = {"query": "x", "instance": B, "index": "vet"}
    assert http_status(search_routes.run_marqo_search(body, member(A, "content_curator"))) == 404


def test_search_own_tenant_returns_own_hits(seeded, fake_marqo):
    fake_marqo.add_index("t-tenant-a-vet", [_hit("1", "d-a", A, "a")])
    result = asyncio.run(search_routes.run_marqo_search({"query": "milk"}, member(A, "viewer")))
    assert result["final_count"] == 1
    assert result["hits"][0]["instance"] == A


@pytest.mark.parametrize(
    "caller, body",
    [
        (master_admin(), {}),
        (member("no-index-tenant", "content_curator"), {}),
        (member("no-index-tenant", "content_curator"), {"instance": "no-index-tenant"}),
        (member({"no-idx-a": ["viewer"], "no-idx-b": ["viewer"]}), {}),
    ],
    ids=["platform-admin", "implicit-scope", "explicit-own-instance", "two-scopes"],
)
def test_search_with_no_own_index_returns_empty_never_default(seeded, fake_marqo, caller, body):
    """BUG 1 leak regression. A caller whose tenant(s) have no registered index (or,
    for the control-plane admin, no data scope at all) gets EMPTY and never resolves
    to, or queries, the default tenant's legacy index. The pre-fix code fell back to
    ``default_physical_index()`` and returned its documents. (A real HTTP call by a
    pure master_admin is also stopped by the ``search`` gate; the handler is called
    directly, so this pins the deeper invariant.)"""
    leak_index = _legacy_default_index(fake_marqo, _hit("1", "d-default", "default", "default-secret"))
    assert _control_probe_lands_on_the_fake(fake_marqo) == leak_index

    result = asyncio.run(search_routes.run_marqo_search({"query": "milk", **body}, caller))
    assert result["effective_config"]["index_name"] is None
    assert result["hits"] == []
    assert result["final_count"] == 0
    assert fake_marqo.searches == []
    assert fake_marqo.constructions == []


def test_summary_empty_tenant_returns_zeros_not_500(seeded):
    """BUG 2 regression: over an empty result set every ``SUM(CASE ...)`` is NULL;
    without COALESCE those fail the int fields of ``DocumentCohortsResponse`` (500)."""
    counts = db_mod.get_document_summary_counts(instances=["empty-tenant"])
    assert counts["total_documents"] == 0
    assert all(v is not None for v in counts.values())
    assert all(isinstance(v, int) for v in counts.values())

    payload = asyncio.run(document_routes.get_documents_summary(member("empty-tenant", "content_curator"), **NO_HEADERS))
    DocumentCohortsResponse(**payload)  # raises on a None int field pre-fix
    assert payload["total_documents"] == 0
    assert payload["review_queue"] == 0
    assert payload["failed_documents"] == 0


def test_search_legacy_index_fails_closed_for_default_tenant(seeded, fake_marqo, monkeypatch):
    """A default-tenant viewer on a legacy index must not see other tenants' hits."""
    _legacy_default_index(
        fake_marqo,
        _hit("1", "d-default", "default", "default-secret"),
        _hit("2", "d-a", A, "tenant-a-secret"),
        _hit("3", "d-b", B, "tenant-b-secret"),
    )
    monkeypatch.delenv("ALLOW_UNSCOPED_LEGACY_SEARCH", raising=False)

    result = asyncio.run(search_routes.run_marqo_search({"query": "secret"}, member("default", "viewer")))
    assert result["hits"] == []
    assert result["effective_config"]["tenant_scope"] == "blocked_legacy_index"
    assert indexes.legacy_search_blocked(result["effective_config"]["filter_string"])
    assert fake_marqo.searches, "fail-closed still queries Marqo with a match-nothing filter"


def test_search_legacy_index_override_is_opt_in(seeded, fake_marqo, monkeypatch):
    """ALLOW_UNSCOPED_LEGACY_SEARCH restores the old unfiltered read, off by default."""
    _legacy_default_index(
        fake_marqo,
        _hit("1", "d-default", "default", "default-secret"),
        _hit("2", "d-a", A, "tenant-a-secret"),
    )
    monkeypatch.setenv("ALLOW_UNSCOPED_LEGACY_SEARCH", "true")
    result = asyncio.run(search_routes.run_marqo_search({"query": "secret"}, member("default", "viewer")))
    assert "tenant-a-secret" in {hit["text"] for hit in result["hits"]}
    assert result["effective_config"]["tenant_scope"] == "unscoped_legacy_override"

    monkeypatch.setenv("ALLOW_UNSCOPED_LEGACY_SEARCH", "false")
    blocked = asyncio.run(search_routes.run_marqo_search({"query": "secret"}, member("default", "viewer")))
    assert blocked["hits"] == []
    assert blocked["effective_config"]["tenant_scope"] == "blocked_legacy_index"


def test_marqo_instance_filter_fails_closed_on_legacy_index_for_restricted():
    """Restricted search must match NOTHING when the index has no ``instance``
    field, the default tenant included (it once failed open, #134). Returning None
    would be an unfiltered read of the whole corpus."""
    legacy, tenant_index = FakeIndex(fields={"text"}), FakeIndex(fields={"instance", "text"})
    # The block is on `doc_id`, NOT `instance`: naming the absent field is exactly
    # what made Marqo 400 on the legacy index (#55).
    block = indexes.LEGACY_UNSCOPED_BLOCK_FILTER
    assert indexes.marqo_instance_filter(member(A, "viewer"), legacy) == block
    assert indexes.marqo_instance_filter(member("default", "viewer"), legacy) == block
    assert indexes.marqo_instance_filter(member({"default": ["viewer"], A: ["viewer"]}), legacy) == block
    assert "instance:(tenant-a)" in indexes.marqo_instance_filter(member(A, "viewer"), tenant_index)
    # Unrestricted / bypass keeps the no-filter behaviour on a legacy index.
    assert indexes.marqo_instance_filter(local_bypass_user(), legacy) is None


def test_marqo_instance_filter_unscoped_override_is_opt_in(monkeypatch):
    """The emergency env restores None (unfiltered) for restricted callers on legacy indexes."""
    legacy = FakeIndex(fields={"text"})
    monkeypatch.setenv("ALLOW_UNSCOPED_LEGACY_SEARCH", "true")
    assert indexes.marqo_instance_filter(member("default", "viewer"), legacy) is None
    monkeypatch.setenv("ALLOW_UNSCOPED_LEGACY_SEARCH", "false")
    assert indexes.marqo_instance_filter(member("default", "viewer"), legacy) == indexes.LEGACY_UNSCOPED_BLOCK_FILTER


def test_index_summary_scoped_to_caller_tenant(seeded, fake_marqo):
    """A tenant caller sees only its own index summaries; unrestricted sees all."""
    for wf, name, count in ((WF_A, "t-tenant-a-vet", 3), (WF_B, "t-tenant-b-vet", 5)):
        fake_marqo.add_index(name)
        db_mod.upsert_document_index_status(workflow_id=wf, index_name=name, status="indexed", chunk_count_indexed=count)

    def summary(user):
        return asyncio.run(search_routes.get_marqo_indexes_summary(user, **NO_HEADERS))

    assert {r["index_name"] for r in summary(member(A, "content_curator"))} == {"t-tenant-a-vet"}
    assert {r["index_name"] for r in summary(local_bypass_user())} == {"t-tenant-a-vet", "t-tenant-b-vet"}
    assert summary(master_admin()) == []  # empty data scope


def test_disable_document_deletes_from_own_tenant_index_not_legacy(seeded, fake_marqo):
    """WF_A's chunks are purged from tenant-A's index, never from the legacy
    ``documents-index`` (which holds the DEFAULT tenant's records)."""
    own = fake_marqo.add_index("t-tenant-a-vet", [{"_id": "a1", "doc_id": "d-a", "workflow_id": WF_A, "instance": A}])
    decoy = fake_marqo.add_index("documents-index", [{"_id": "x1", "doc_id": "d-a", "instance": "default"}])

    res = asyncio.run(document_routes.disable_document(WF_A, member(A, "admin"), remove_from_search=True))

    assert own.records == []
    assert res["marqo_deleted"] == 1
    assert len(decoy.records) == 1
    searched = {name for name, _ in fake_marqo.searches}
    assert "t-tenant-a-vet" in searched
    assert "documents-index" not in searched


# --- indexes + tenant provisioning (/tenants/*, platform gate) -------------------


def test_list_other_tenant_indexes_is_404(seeded):
    assert http_status(tenant_routes.list_tenant_indexes(B, member(A, "content_curator"))) == 404


def test_create_index_under_other_tenant_is_404(seeded, fake_marqo):
    assert http_status(tenant_routes.create_tenant_index(B, {"name": "schemes"}, member(A, "content_curator"))) == 404


def test_delete_index_under_other_tenant_is_404(seeded, fake_marqo):
    assert http_status(tenant_routes.delete_tenant_index(B, "vet", member(A, "admin"), force=False)) == 404


def test_tenant_admin_is_scoped_to_own_tenant(seeded, fake_marqo):
    """A per-tenant admin manages its own tenant and is hidden from the other (404)."""
    tadmin = member(A, "admin")
    assert {r["name"] for r in asyncio.run(tenant_routes.list_tenant_indexes(A, tadmin))} == {"vet"}
    created = asyncio.run(tenant_routes.create_tenant_index(A, {"name": "schemes"}, tadmin))
    assert created["marqo_index"] == "t-tenant-a-schemes"
    assert http_status(tenant_routes.list_tenant_indexes(B, tadmin)) == 404
    assert http_status(tenant_routes.create_tenant_index(B, {"name": "schemes"}, tadmin)) == 404


def test_tenant_admin_cannot_hit_platform_create_tenant(seeded):
    """POST /tenants is gated by the ``require_platform_admin`` dependency."""
    assert http_status(require_platform_admin(member(A, "admin"))) == 403
    # A real platform admin passes the control-plane gate but is NOT data-unrestricted.
    assert asyncio.run(require_platform_admin(master_admin())).is_platform_admin is True


def test_platform_admin_cannot_reach_tenant_index_management(seeded, fake_marqo):
    """A tenant's indexes are data plane. The control-plane admin is no member of
    either tenant, so it is denied with 403 (it knows the tenant exists)."""
    admin = master_admin()
    assert http_status(tenant_routes.list_tenant_indexes(A, admin)) == 403
    assert http_status(tenant_routes.create_tenant_index(A, {"name": "schemes"}, admin)) == 403
    assert http_status(tenant_routes.delete_tenant_index(A, "vet", admin, force=False)) == 403


def test_upload_create_instance_requires_upload_in_that_tenant(seeded):
    """A viewer-in-A / curator-in-B passes the any-instance RequireUpload gate but
    must NOT be able to create documents into A."""
    mixed = member({A: ["viewer"], B: ["content_curator"]})
    assert access.resolve_create_instance(mixed, B) == B
    with pytest.raises(HTTPException) as exc:  # reachable tenant, wrong role
        access.resolve_create_instance(mixed, A)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:  # unreachable tenant: existence hidden
        access.resolve_create_instance(mixed, "tenant-z")
    assert exc.value.status_code == 404


def test_deleted_doc_audit_not_visible_to_default_tenant(db_connection):
    db = db_connection
    default = db._default_instance_id()
    seed_document(db, "wf-def", document_id="d-def", filename="def.pdf", stage="ocr_review", instance=default)
    db.log_audit(
        workflow_id="wf-def", document_id="d-def", action_type="edit",
        field_name="page", old_value="def-old", new_value="def-new",
    )
    # An ORPHAN audit row whose document was deleted carries that document's
    # content and must NOT be attributed to the default tenant (the LEFT JOIN +
    # COALESCE(..., DEFAULT) bug).
    db.log_audit(
        workflow_id="wf-ghost", document_id="d-ghost", action_type="edit",
        field_name="chunk", old_value="OTHER-TENANT-SECRET", new_value="x",
    )

    wfids = {row["workflow_id"] for row in db.get_all_audit_logs(instances=[default])}
    assert "wf-def" in wfids
    assert "wf-ghost" not in wfids
    assert db.get_all_audit_log_count(instances=[default]) == 1
    # Unrestricted callers keep the LEFT JOIN and still see the orphan row.
    assert {"wf-def", "wf-ghost"} <= {row["workflow_id"] for row in db.get_all_audit_logs(instances=None)}
