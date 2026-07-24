"""Cross-tenant isolation harness — the Phase 6 definition-of-done guardrail.

An automated cross-tenant probe on every data plane. The invariant under test
(TENANT_ISOLATION_PLAN.md §6): *a tenant-A token gets 404/empty on every
tenant-B doc, chunk, artifact URL, run, and search.* Existence is never leaked,
so cross-tenant access surfaces as **404** (not 403); a reachable tenant with an
insufficient role is **403**.

Each plane is a separate, individually-named test. Following the existing
suite (``test_tenancy.py`` / ``test_tenant_indexes.py``), the route *handler
functions* are called directly with mocked module clients rather than through a
live ``TestClient`` — the app lifespan needs a Temporal connection. Because a
direct call bypasses FastAPI dependency resolution, explicit values are passed
for every ``Query()`` / ``Header()`` parameter, and dependency-enforced gates
(the platform-admin gate) are exercised by invoking the dependency directly.

Principals (see ``pipeline/auth/models.py``):
  * ``_platform_admin``    — realm ``master_admin`` → CONTROL PLANE only. Manages
    the tenant registry; holds NO data permissions and reaches NO tenant's data.
  * ``_tenant_admin_in(t)``— ``tenant_roles={t:[admin]}`` → full inside ``t`` only.
  * ``_curator_in(t)``     — ``tenant_roles={t:[content_curator]}``.
  * ``_viewer_in(t)``      — ``tenant_roles={t:[viewer]}`` (search-only).
"""

from __future__ import annotations

import asyncio
import re
from unittest.mock import MagicMock

import marqo  # real module; we monkeypatch ``.Client`` for search tests
import pytest
from fastapi import HTTPException

import pipeline.api as api
import pipeline.db as db_mod
from pipeline.auth.deps import require_platform_admin
from pipeline.auth.jwt import claims_to_user
from pipeline.models import PageUpdate, ChunkUpdate


def _run(coro):
    return asyncio.run(coro)


# --- test principals ---------------------------------------------------------


def _platform_admin():
    """Control-plane super-admin (realm ``master_admin``): tenants only, NO data."""
    return claims_to_user({"sub": "root", "realm_access": {"roles": ["master_admin"]}})


def _tenant_admin_in(instance: str):
    """Tenant-scoped admin: ``admin`` inside ``instance`` only (NOT platform-wide)."""
    return claims_to_user({"sub": "tadmin", "tenant_roles": {instance: ["admin"]}})


def _curator_in(instance: str):
    return claims_to_user({"sub": "cur", "tenant_roles": {instance: ["content_curator"]}})


def _viewer_in(instance: str):
    return claims_to_user({"sub": "view", "tenant_roles": {instance: ["viewer"]}})


# --- fake Marqo (physical index-per-tenant + tolerant instance filter) --------

# physical index name -> list of chunk-hit dicts (each carries an ``instance``)
_INDEX_HITS: dict[str, list[dict]] = {}
# physical indexes that "exist" in this fake Marqo (realistic get_index semantics:
# creating a brand-new index name must not report it as pre-existing).
_EXISTING_INDEXES: set[str] = set()
# physical indexes that DON'T advertise the filterable `instance` field — i.e. the
# legacy single-tenant index. For these the tolerant per-chunk filter is skipped,
# so a mis-resolved cross-tenant read would leak documents (the live BUG 1 shape).
_LEGACY_INDEXES: set[str] = set()
# records of (physical_index_name, search_kwargs) for assertions
_SEARCH_CALLS: list[tuple] = []


def _instances_in_filter(filter_string: str):
    """Set of instances an ``instance:(...)`` filter admits, or None if absent."""
    groups = re.findall(r"instance:\(([^)]*)\)", filter_string or "")
    if not groups:
        return None
    allowed: set[str] = set()
    for grp in groups:
        allowed.update(part.strip() for part in grp.split(" OR ") if part.strip())
    return allowed


class _FakeIndex:
    def __init__(self, name):
        self.name = name

    def get_settings(self):
        # Advertise the filterable ``instance`` field so the tolerant per-chunk
        # filter engages for restricted callers — UNLESS this index is registered
        # as legacy (no ``instance`` field), in which case the filter is skipped
        # (the live single-tenant index shape that makes a mis-resolve leak).
        fields = [
            {"name": "text"},
            {"name": "domain_tags"},
            {"name": "is_reference"},
        ]
        if self.name not in _LEGACY_INDEXES:
            fields.insert(0, {"name": "instance"})
        return {"allFields": fields}

    def get_stats(self):
        return {"numberOfDocuments": len(_INDEX_HITS.get(self.name, []))}

    def get_document(self, _id):
        for hit in _INDEX_HITS.get(self.name, []):
            if hit.get("_id") == _id:
                return hit
        raise Exception("document not found")

    def search(self, **kwargs):
        _SEARCH_CALLS.append((self.name, kwargs))
        hits = list(_INDEX_HITS.get(self.name, []))
        allowed = _instances_in_filter(kwargs.get("filter_string"))
        if allowed is not None:
            # Emulate Marqo honouring the tenant filter clause.
            hits = [h for h in hits if h.get("instance") in allowed]
        return {"hits": hits}


class _FakeClient:
    def __init__(self, url=None, **kwargs):
        self.url = url

    def index(self, name):
        return _FakeIndex(name)

    def get_index(self, name):
        # Realistic: an index only "exists" once it has been created (or seeded with
        # hits). A never-created name raises, exactly as Marqo does — so provisioning
        # a fresh index is not mistaken for adopting a pre-existing physical index.
        if name in _EXISTING_INDEXES or name in _INDEX_HITS:
            return _FakeIndex(name)
        raise Exception(f"index {name} not found")

    def create_index(self, name, settings_dict=None):
        _EXISTING_INDEXES.add(name)
        return {"acknowledged": True}

    def delete_index(self, name):
        _EXISTING_INDEXES.discard(name)
        _INDEX_HITS.pop(name, None)
        return {"acknowledged": True}


@pytest.fixture
def marqo_stub(monkeypatch):
    """Patch the ``marqo`` module client and reset the in-memory index store."""
    _INDEX_HITS.clear()
    _EXISTING_INDEXES.clear()
    _LEGACY_INDEXES.clear()
    _SEARCH_CALLS.clear()
    monkeypatch.setattr(marqo, "Client", _FakeClient)
    return _INDEX_HITS


# --- seed two tenants with docs/chunks/artifacts/runs/indexes ----------------

A = "tenant-a"
B = "tenant-b"
WF_A = "wf-a"
WF_B = "wf-b"


def _seed(db):
    """Populate two isolated tenants and return handy ids."""
    for wf, doc_id, inst in ((WF_A, "d-a", A), (WF_B, "d-b", B)):
        db.upsert_document(
            workflow_id=wf,
            document_id=doc_id,
            filename=f"{inst}-secret.pdf",
            filepath=f"/tmp/{wf}.pdf",
            stage="ocr_review",
            instance=inst,
        )
        db.save_pages(wf, [{"page_number": 1, "original_markdown": f"{inst} page"}])
        db.save_chunks(
            wf,
            [{"chunk_number": 1, "original_text": f"{inst} chunk body", "source_pages": [1]}],
        )
        db.log_audit(
            workflow_id=wf, document_id=doc_id, action_type="approval",
            field_name="ocr_approved", new_value="True",
        )

    art_a = db.add_document_artifact(WF_A, "original_upload", "/tmp/a.bin", filename="a.bin")
    art_b = db.add_document_artifact(WF_B, "original_upload", "/tmp/b.bin", filename="b.bin")
    run_a = db.create_document_job(workflow_id=WF_A, job_type="pipeline")
    run_b = db.create_document_job(workflow_id=WF_B, job_type="pipeline")

    # Registry: a default index per tenant.
    db.create_index_row(A, "vet", "t-tenant-a-vet", is_default=True)
    db.create_index_row(B, "vet", "t-tenant-b-vet", is_default=True)
    return {"art_a": art_a, "art_b": art_b, "run_a": run_a, "run_b": run_b}


@pytest.fixture
def seeded(db_connection, monkeypatch):
    """db_connection + api bound to it + two seeded tenants."""
    monkeypatch.setattr(api, "db", db_mod)
    ids = _seed(db_mod)
    return ids


def _status(exc):
    return exc.value.status_code


# =============================================================================
# Plane: Documents (list / summary / detail)
# =============================================================================


def test_documents_list_excludes_other_tenant(seeded):
    rows = _run(api.list_documents(
        _curator_in(A), stage=None, limit=100, offset=0,
        x_include_demo=None, x_include_disabled=None,
    ))
    wids = {r.workflow_id for r in rows}
    assert wids == {WF_A}
    assert WF_B not in wids


def test_documents_summary_excludes_other_tenant(seeded):
    summary = _run(api.get_documents_summary(
        _curator_in(A), x_include_demo=None, x_include_disabled=None,
    ))
    assert summary["total_documents"] == 1


def test_get_document_cross_tenant_is_404(seeded):
    with pytest.raises(HTTPException) as exc:
        _run(api.get_document(WF_B, _viewer_in(A)))
    assert _status(exc) == 404  # not 403 — existence not leaked


def test_get_own_document_succeeds(seeded):
    detail = _run(api.get_document(WF_A, _viewer_in(A)))
    assert detail.workflow_id == WF_A


def test_platform_admin_sees_no_tenant_data(seeded):
    """Control-plane super-admin has NO data access: the document plane is empty
    and any specific tenant document is hidden (404), same as a non-member."""
    rows = _run(api.list_documents(
        _platform_admin(), stage=None, limit=100, offset=0,
        x_include_demo=None, x_include_disabled=None,
    ))
    assert rows == []
    with pytest.raises(HTTPException) as exc:
        _run(api.get_document(WF_B, _platform_admin()))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.get_document(WF_A, _platform_admin()))
    assert _status(exc) == 404


# =============================================================================
# Plane: Doc sub-resources (pages / chunks / artifacts / pdf / export / audit)
# =============================================================================


def test_pages_cross_tenant_is_404(seeded):
    viewer = _viewer_in(A)
    with pytest.raises(HTTPException) as exc:
        _run(api.list_pages(WF_B, viewer))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.get_page(WF_B, viewer, page_num=1))
    assert _status(exc) == 404


def test_chunks_cross_tenant_is_404(seeded):
    viewer = _viewer_in(A)
    with pytest.raises(HTTPException) as exc:
        _run(api.list_chunks(WF_B, viewer, include_excluded=False))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.get_chunk(WF_B, viewer, chunk_num=1))
    assert _status(exc) == 404


def test_artifacts_cross_tenant_is_404(seeded):
    viewer = _viewer_in(A)
    art_b = seeded["art_b"]
    with pytest.raises(HTTPException) as exc:
        _run(api.list_document_artifacts(WF_B, viewer))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.get_document_artifact(WF_B, viewer, art_b))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.get_document_artifact_content(WF_B, viewer, art_b))
    assert _status(exc) == 404


def test_pdf_cross_tenant_is_404(seeded):
    with pytest.raises(HTTPException) as exc:
        _run(api.get_document_pdf(WF_B, _viewer_in(A)))
    assert _status(exc) == 404


def test_export_cross_tenant_is_404(seeded):
    viewer = _viewer_in(A)
    with pytest.raises(HTTPException) as exc:
        _run(api.export_markdown(WF_B, viewer))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.export_chunks(WF_B, viewer, include_excluded=False))
    assert _status(exc) == 404


def test_per_document_audit_cross_tenant_is_404(seeded):
    with pytest.raises(HTTPException) as exc:
        _run(api.get_document_audit_log(WF_B, _viewer_in(A), action_type=None, limit=50, offset=0))
    assert _status(exc) == 404


def test_global_audit_list_excludes_other_tenant(seeded):
    """Global ``/audit`` must be instance-scoped (regression: it was not — see report)."""
    resp = _run(api.get_all_audit_logs(_viewer_in(A), action_type=None, limit=50, offset=0))
    wids = {entry.workflow_id for entry in resp.logs}
    assert wids == {WF_A}
    assert WF_B not in wids
    assert resp.total == 1
    # Control-plane admin has no data scope -> the global audit trail is empty.
    admin_resp = _run(api.get_all_audit_logs(_platform_admin(), action_type=None, limit=50, offset=0))
    assert admin_resp.logs == []
    assert admin_resp.total == 0


def test_provenance_chunk_cross_tenant_is_404(seeded):
    with pytest.raises(HTTPException) as exc:
        _run(api.resolve_provenance_chunk(
            MagicMock(), _viewer_in(A), doc_id=WF_B, chunk_num=1, marqo_id=None,
            index_name="documents-index",
        ))
    assert _status(exc) == 404


def test_own_doc_subresources_and_provenance_ok(seeded):
    """Positive control: the tenant-a caller reaches its own sub-resources."""
    viewer = _viewer_in(A)
    assert len(_run(api.list_pages(WF_A, viewer))) == 1
    assert len(_run(api.list_chunks(WF_A, viewer, include_excluded=False))) == 1
    assert {a["id"] for a in _run(api.list_document_artifacts(WF_A, viewer))} == {seeded["art_a"]}
    assert "content" in _run(api.export_markdown(WF_A, viewer))
    prov = _run(api.resolve_provenance_chunk(
        MagicMock(), viewer, doc_id=WF_A, chunk_num=1, marqo_id=None,
        index_name="documents-index",
    ))
    assert prov["workflow_id"] == WF_A


# =============================================================================
# Plane: Doc-scoped Marqo status (/documents/{}/marqo*)
# =============================================================================


def test_document_marqo_status_cross_tenant_is_404(seeded):
    viewer = _viewer_in(A)
    with pytest.raises(HTTPException) as exc:
        _run(api.get_document_marqo_status(WF_B, viewer, index_name="documents-index"))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.list_document_marqo_chunks(WF_B, viewer, index_name="documents-index"))
    assert _status(exc) == 404


def test_document_marqo_targeting_other_tenant_index_is_404(seeded):
    """Own doc, but a tenant-B physical index name -> hidden as 404."""
    with pytest.raises(HTTPException) as exc:
        _run(api.get_document_marqo_status(WF_A, _curator_in(A), index_name="t-tenant-b-vet"))
    assert _status(exc) == 404


# =============================================================================
# Plane: Mutations (approve / retry / reingest / patch)
# =============================================================================


def test_mutation_cross_tenant_is_404(seeded):
    curator = _curator_in(A)
    with pytest.raises(HTTPException) as exc:
        _run(api.approve_ocr(WF_B, curator))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.reingest_document(WF_B, curator, marqo_url="", index_name="documents-index"))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.retry_ocr(WF_B, curator))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.update_page(WF_B, PageUpdate(), curator, page_num=1))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.update_chunk(WF_B, ChunkUpdate(), curator, chunk_num=1))
    assert _status(exc) == 404


def test_mutation_wrong_role_in_own_tenant_is_403(seeded):
    """viewer_in(tenant-a) may READ tenant-a but not mutate it -> 403 (not 404)."""
    viewer = _viewer_in(A)
    with pytest.raises(HTTPException) as exc:
        _run(api.approve_ocr(WF_A, viewer))
    assert _status(exc) == 403
    with pytest.raises(HTTPException) as exc:
        _run(api.reingest_document(WF_A, viewer, marqo_url="", index_name="documents-index"))
    assert _status(exc) == 403
    with pytest.raises(HTTPException) as exc:
        _run(api.update_page(WF_A, PageUpdate(), viewer, page_num=1))
    assert _status(exc) == 403


# =============================================================================
# Plane: Runs (/runs, /operations/queue, /runs/{id})
# =============================================================================


def test_runs_list_excludes_other_tenant(seeded):
    runs = _run(api.list_runs(_curator_in(A), limit=100, offset=0, status=None))
    assert {r["workflow_id"] for r in runs} == {WF_A}


def test_operations_queue_excludes_other_tenant(seeded):
    resp = _run(api.get_operations_queue(
        _curator_in(A), limit=100, offset=0, x_include_demo=None, x_include_disabled=None,
    ))
    assert {item.workflow_id for item in resp.items} == {WF_A}
    assert resp.total == 1


def test_get_run_cross_tenant_is_404(seeded):
    with pytest.raises(HTTPException) as exc:
        _run(api.get_run(seeded["run_b"], _curator_in(A)))
    assert _status(exc) == 404


def test_own_run_accessible_and_platform_admin_sees_none(seeded):
    curator = _curator_in(A)
    assert _run(api.get_run(seeded["run_a"], curator))["workflow_id"] == WF_A
    # Control-plane admin has no data scope -> sees no runs, and a specific run
    # is hidden (404) just like for any non-member.
    admin_runs = _run(api.list_runs(_platform_admin(), limit=100, offset=0, status=None))
    assert admin_runs == []
    with pytest.raises(HTTPException) as exc:
        _run(api.get_run(seeded["run_a"], _platform_admin()))
    assert _status(exc) == 404


# =============================================================================
# Plane: Search (/marqo/search)
# =============================================================================


def test_search_resolves_to_own_index_and_filters_out_other_tenant(seeded, marqo_stub):
    # tenant-a's physical index accidentally holds a tenant-b chunk too — the
    # tolerant per-chunk instance filter must still strip it.
    marqo_stub["t-tenant-a-vet"] = [
        {"_id": "1", "doc_id": "d-a", "instance": A, "text": "a"},
        {"_id": "2", "doc_id": "d-b", "instance": B, "text": "b-leak"},
    ]
    result = _run(api.run_marqo_search({"query": "milk"}, _curator_in(A)))
    # 1) resolved to the tenant's own physical index
    assert result["effective_config"]["index_name"] == "t-tenant-a-vet"
    # 2) an instance filter clause was applied
    assert "instance:(tenant-a)" in (result["effective_config"]["filter_string"] or "")
    # 3) no tenant-b hit survives
    assert {h["instance"] for h in result["hits"]} == {A}


def test_search_targeting_other_tenant_physical_index_is_404(seeded, marqo_stub):
    with pytest.raises(HTTPException) as exc:
        _run(api.run_marqo_search({"query": "x", "index_name": "t-tenant-b-vet"}, _curator_in(A)))
    assert _status(exc) == 404


def test_search_targeting_other_tenant_instance_is_403(seeded, marqo_stub):
    with pytest.raises(HTTPException) as exc:
        _run(api.run_marqo_search({"query": "x", "instance": B, "index": "vet"}, _curator_in(A)))
    assert _status(exc) == 403


def test_search_platform_admin_has_no_data(seeded, marqo_stub):
    """The control-plane admin has an empty data scope: search yields nothing.

    (A real HTTP call is additionally blocked upfront by the ``search`` gate,
    which a pure master_admin lacks; calling the handler directly bypasses that
    dep, so we assert the deeper invariant — the tenant filter admits nothing.)"""
    marqo_stub["documents-index"] = [
        {"_id": "1", "doc_id": "d-a", "instance": A, "text": "a"},
        {"_id": "2", "doc_id": "d-b", "instance": B, "text": "b"},
    ]
    result = _run(api.run_marqo_search({"query": "milk"}, _platform_admin()))
    # An empty-scope caller matches nothing (never an unfiltered all-tenant read).
    assert "instance:(__none__)" in (result["effective_config"]["filter_string"] or "")
    assert result["hits"] == []


def test_search_own_tenant_returns_own_hits(seeded, marqo_stub):
    marqo_stub["t-tenant-a-vet"] = [{"_id": "1", "doc_id": "d-a", "instance": A, "text": "a"}]
    result = _run(api.run_marqo_search({"query": "milk"}, _viewer_in(A)))
    assert result["final_count"] == 1
    assert result["hits"][0]["instance"] == A


# --- BUG 1 leak regression: a tenant with NO index must never read another's ----


def test_search_tenant_with_no_index_returns_empty_never_default(seeded, marqo_stub):
    """BUG 1 leak regression (implicit-scope entry point).

    A restricted caller whose tenant has NO registered index issues a plain search
    (no explicit instance/index in the body -> resolved from the caller's single
    scope). The legacy/default-tenant physical index holds real documents and is
    LEGACY-style (no ``instance`` field, so the tolerant per-chunk filter is
    skipped) — exactly the live leak condition. The API must return EMPTY and must
    NEVER resolve to, or query, the default tenant's index.

    Fails on the pre-fix code (which fell back to ``_default_physical_index()`` and
    returned the default tenant's document); passes after the fix.
    """
    leak_index = api._default_physical_index()
    _LEGACY_INDEXES.add(leak_index)
    marqo_stub[leak_index] = [
        {"_id": "1", "doc_id": "d-default", "instance": "default", "text": "default-secret"},
    ]
    # ``no-index-tenant`` is a real, accessible tenant for this caller but has no
    # registered index (never seeded in the registry).
    result = _run(api.run_marqo_search({"query": "milk"}, _curator_in("no-index-tenant")))
    # 1) no default-tenant physical index name is leaked in the effective config
    assert result["effective_config"]["index_name"] is None
    # 2) empty result — the default tenant's document is NOT returned
    assert result["hits"] == []
    assert result["final_count"] == 0
    # 3) Marqo was never queried — no fallback index was touched at all
    assert _SEARCH_CALLS == []


def test_search_explicit_own_instance_with_no_index_returns_empty(seeded, marqo_stub):
    """BUG 1 leak regression (explicit-instance entry point).

    Same leak via ``assert_index_access``: the caller explicitly names its OWN
    (index-less) instance in the body. This must also return empty and never fall
    back to the default tenant's index.
    """
    leak_index = api._default_physical_index()
    _LEGACY_INDEXES.add(leak_index)
    marqo_stub[leak_index] = [
        {"_id": "1", "doc_id": "d-default", "instance": "default", "text": "default-secret"},
    ]
    result = _run(api.run_marqo_search(
        {"query": "milk", "instance": "no-index-tenant"}, _curator_in("no-index-tenant"),
    ))
    assert result["effective_config"]["index_name"] is None
    assert result["hits"] == []
    assert _SEARCH_CALLS == []


# --- BUG 2 regression: empty tenant summary must be zeros, not a 500 ------------


def test_summary_empty_tenant_returns_zeros_not_500(seeded):
    """BUG 2 regression: a tenant with zero documents.

    Over an empty result set every ``SUM(CASE ...)`` is NULL (only COUNT() is 0);
    without COALESCE those NULLs fail the int fields of ``DocumentCohortsResponse``
    -> 500. After the fix the counts are all-zero ints and the route validates.
    """
    from pipeline.models import DocumentCohortsResponse

    counts = db_mod.get_document_summary_counts(instances=["empty-tenant"])
    assert counts["total_documents"] == 0
    # No NULLs anywhere — every field is a real int (would be None pre-fix).
    assert all(v is not None for v in counts.values())
    assert all(isinstance(v, int) for v in counts.values())

    # The route builds + returns a DocumentCohortsResponse-shaped dict; validating
    # it exercises the same int coercion that 500'd pre-fix (the 200 path).
    payload = _run(api.get_documents_summary(
        _curator_in("empty-tenant"), x_include_demo=None, x_include_disabled=None,
    ))
    DocumentCohortsResponse(**payload)  # raises on a None int field pre-fix
    assert payload["total_documents"] == 0
    assert payload["review_queue"] == 0
    assert payload["failed_documents"] == 0


# =============================================================================
# Plane: Indexes + tenant provisioning (/tenants/*, platform gate)
# =============================================================================


def test_list_other_tenant_indexes_is_404(seeded):
    with pytest.raises(HTTPException) as exc:
        _run(api.list_tenant_indexes(B, _curator_in(A)))
    assert _status(exc) == 404


def test_create_index_under_other_tenant_is_404(seeded, marqo_stub):
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_index(B, {"name": "schemes"}, _curator_in(A)))
    assert _status(exc) == 404


def test_delete_index_under_other_tenant_is_404(seeded, marqo_stub):
    with pytest.raises(HTTPException) as exc:
        _run(api.delete_tenant_index(B, "vet", _tenant_admin_in(A), force=False))
    assert _status(exc) == 404


def test_tenant_admin_is_scoped_to_own_tenant(seeded, marqo_stub):
    """The corrected model: a per-tenant admin manages only its own tenant."""
    tadmin = _tenant_admin_in(A)
    # Can view + create within tenant-a...
    assert {r["name"] for r in _run(api.list_tenant_indexes(A, tadmin))} == {"vet"}
    created = _run(api.create_tenant_index(A, {"name": "schemes"}, tadmin))
    assert created["marqo_index"] == "t-tenant-a-schemes"
    # ...but is hidden from tenant-b entirely (404, no existence leak).
    with pytest.raises(HTTPException) as exc:
        _run(api.list_tenant_indexes(B, tadmin))
    assert _status(exc) == 404
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_index(B, {"name": "schemes"}, tadmin))
    assert _status(exc) == 404


def test_tenant_admin_cannot_hit_platform_create_tenant(seeded):
    """POST /tenants is gated by ``require_platform_admin`` (master_admin only).

    The gate is a FastAPI dependency, so it is exercised directly (a raw handler
    call would bypass dependency resolution).
    """
    with pytest.raises(HTTPException) as exc:
        _run(require_platform_admin(_tenant_admin_in(A)))
    assert _status(exc) == 403
    # A real platform admin passes the control-plane gate (but is NOT
    # data-unrestricted — that is a separate, data-scope property).
    passed = _run(require_platform_admin(_platform_admin()))
    assert passed.is_platform_admin is True
    assert passed.is_instance_unrestricted() is False


def test_platform_admin_cannot_reach_tenant_index_management(seeded, marqo_stub):
    """A tenant's indexes are data-plane. The control-plane admin is not a member
    of either tenant, so listing/creating/deleting a tenant's indexes is denied
    with 403 (it knows the tenant exists; it just has no data role there)."""
    admin = _platform_admin()
    with pytest.raises(HTTPException) as exc:
        _run(api.list_tenant_indexes(A, admin))
    assert _status(exc) == 403
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_index(A, {"name": "schemes"}, admin))
    assert _status(exc) == 403
    with pytest.raises(HTTPException) as exc:
        _run(api.delete_tenant_index(A, "vet", admin, force=False))
    assert _status(exc) == 403
