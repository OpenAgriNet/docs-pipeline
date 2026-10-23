"""Route contract guards for the assembled app. Breaking either fails silently.

**A. Route ordering.** Starlette dispatches to the first ``Match.FULL`` in
registration order and every ``{workflow_id}`` / ``{instance}`` parameter is an
unconstrained ``str``, so ``/documents/{workflow_id}/approve-ocr`` swallows the
literal ``/documents/bulk/approve-ocr`` if its router is included first. Asserted
on actual resolution, not on indices in ``app.routes``.

**B. Permission tiers.** ``test_tenant_isolation`` calls handlers directly and never
runs the dependency graph, so each route's tier is pinned statically AND exercised
through the real ASGI stack with auth on. ``conftest.py`` sets ``AUTH_DISABLED=true``
and ``get_current_user`` re-reads it per request (giving ``local_bypass_user()``, which
nothing rejects), so ``auth_on`` flips it off; only the Keycloak signature check is
stubbed.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.routing import Match

from pipeline import db
from pipeline.app import app
from pipeline.auth import deps as auth_deps
from pipeline.auth.deps import (
    CurrentUser,
    RequireAdmin,
    RequirePipeline,
    RequirePlatformAdmin,
    RequireReview,
    RequireSearch,
    RequireUpload,
)
from pipeline.auth.models import AuthUser
from pipeline.auth.permissions import Permission
from pipeline.services import tenants
from tests.helpers import app_route, dependency_calls


# --- A: literal routes resolve ahead of their parameterized twins ----------------


def _resolve(method: str, path: str):
    """The route Starlette dispatches to: the first ``Match.FULL`` in registration order."""
    scope = {"type": "http", "method": method, "path": path, "root_path": "", "headers": [], "query_string": b""}
    return next((route for route in app.routes if route.matches(scope)[0] == Match.FULL), None)


# (method, literal path, its handler): every literal under a prefix that also has
# parameterized routes. "No twin today" rows are pinned so that adding one (e.g.
# ``POST /documents/{workflow_id}``) trips here instead of hijacking the literal.
LITERAL_ROUTES: list[tuple[str, str, str]] = [
    # --- genuinely ambiguous today (a parameterized twin matches this path) ---
    ("GET", "/documents/summary", "get_documents_summary"),
    ("GET", "/documents/cohorts", "get_document_cohorts"),
    ("POST", "/documents/bulk/approve-ocr", "bulk_approve_ocr"),
    ("POST", "/documents/bulk/approve-translation", "bulk_approve_translation"),
    ("POST", "/documents/bulk/approve-chunks", "bulk_approve_chunks"),
    # --- same shape, no twin today ---
    ("POST", "/documents/batch", "start_batch_workflows"),
    ("POST", "/documents/reconcile", "reconcile_document_states"),
    ("POST", "/documents/bulk/reindex", "bulk_reindex_documents"),
    ("POST", "/documents/bulk/auto-tag", "bulk_auto_tag_documents"),
    ("GET", "/marqo/indexes/summary", "get_marqo_indexes_summary"),
    ("POST", "/tenants/reconcile", "reconcile_tenants_route"),
    ("GET", "/tenants", "list_tenants_route"),
    ("POST", "/tenants", "create_tenant_route"),
]


# (method, literal, literal handler, parameterized template, its handler): the template
# matches the literal path, so registration order alone keeps them apart.
COLLIDING_PAIRS: list[tuple[str, str, str, str, str]] = [
    ("GET", "/documents/summary", "get_documents_summary",
     "/documents/{workflow_id}", "get_document"),
    ("GET", "/documents/cohorts", "get_document_cohorts",
     "/documents/{workflow_id}", "get_document"),
    ("POST", "/documents/bulk/approve-ocr", "bulk_approve_ocr",
     "/documents/{workflow_id}/approve-ocr", "approve_ocr"),
    ("POST", "/documents/bulk/approve-translation", "bulk_approve_translation",
     "/documents/{workflow_id}/approve-translation", "approve_translation"),
    ("POST", "/documents/bulk/approve-chunks", "bulk_approve_chunks",
     "/documents/{workflow_id}/approve-chunks", "approve_chunks"),
]


@pytest.mark.parametrize("method,path,expected_handler", LITERAL_ROUTES, ids=[f"{m}:{p}" for m, p, _ in LITERAL_ROUTES])
def test_literal_route_resolves_to_its_own_handler(method: str, path: str, expected_handler: str):
    route = _resolve(method, path)
    assert route is not None, f"nothing matched {method} {path}"
    assert route.endpoint.__name__ == expected_handler, (
        f"{method} {path} resolved to {route.endpoint.__name__!r} (template {route.path!r}): "
        "a parameterized route was registered first"
    )


@pytest.mark.parametrize(
    "method,literal,literal_handler,param,param_handler", COLLIDING_PAIRS,
    ids=[f"{m}:{lit}" for m, lit, _, _, _ in COLLIDING_PAIRS],
)
def test_literal_route_wins_over_parameterized_twin(
    method: str, literal: str, literal_handler: str, param: str, param_handler: str
):
    """First that the template really matches the literal path (else the row is stale and
    belongs in LITERAL_ROUTES), so the resolution assert cannot pass vacuously."""
    param_route = app_route(method, param)
    assert param_route.endpoint.__name__ == param_handler
    assert param_route.path_regex.match(literal), f"{param} no longer matches {literal}; this collision row is stale"
    resolved = _resolve(method, literal)
    assert resolved is not None
    assert resolved.endpoint.__name__ == literal_handler, (
        f"{method} {literal} was swallowed by {param} -> {resolved.endpoint.__name__}"
    )


def test_no_unlisted_literal_parameterized_collisions():
    """A new literal under an existing parameterized prefix fails here until pinned in COLLIDING_PAIRS."""
    routes = [r for r in app.routes if getattr(r, "path_regex", None) is not None and getattr(r, "methods", None)]
    found = {
        (method, lit.path, par.path)
        for lit in routes if "{" not in lit.path
        for method in lit.methods - {"HEAD", "OPTIONS"}
        for par in routes if "{" in par.path and method in par.methods and par.path_regex.match(lit.path)
    }
    pinned = {(m, lit, par) for m, lit, _, par, _ in COLLIDING_PAIRS}
    assert found == pinned, f"unpinned collisions: {sorted(found - pinned)}; stale rows: {sorted(pinned - found)}"


# --- B: required permission tier per route, exercised through the app ------------

#: tier alias -> (name, the permission it requires; None for the control-plane gate)
TIERS: dict[Any, tuple[str, Permission | None]] = {
    RequireUpload: ("RequireUpload", Permission.UPLOAD),
    RequireReview: ("RequireReview", Permission.REVIEW),
    RequirePipeline: ("RequirePipeline", Permission.PIPELINE),
    RequireSearch: ("RequireSearch", Permission.SEARCH),
    RequireAdmin: ("RequireAdmin", Permission.ADMIN),
    RequirePlatformAdmin: ("RequirePlatformAdmin", None),
}

# Path-parameter values for the live request. The resources do not exist: a tier
# must reject during dependency resolution, before the handler looks anything up.
PATH_PARAMS = {
    "workflow_id": "wf-guard",
    "artifact_id": "1",
    "page_num": "1",
    "chunk_num": "1",
    "index_name": "idx-guard",
    "instance": "tenant-guard",
    "name": "idx-guard",
    "job_id": "1",
    "user_id": "user-guard",
}

# THE PIN: every route carrying a permission tier. Routes on bare ``CurrentUser``
# gate in-handler with instance-aware checks and are covered by test_tenant_isolation.
ROUTE_TIERS: list[tuple[str, str, Any]] = [
    ('POST', '/documents', RequireUpload),
    ('POST', '/upload', RequireUpload),
    ('POST', '/documents/batch', RequireUpload),
    ('GET', '/operations/queue', RequireSearch),
    ('GET', '/runs', RequireSearch),
    ('GET', '/runs/{job_id}', RequireSearch),
    ('GET', '/documents/{workflow_id}/error-details', RequireSearch),
    ('GET', '/documents/{workflow_id}/runtime', RequireSearch),
    ('GET', '/documents/{workflow_id}/artifacts', RequireSearch),
    ('GET', '/documents/{workflow_id}/artifacts/{artifact_id}', RequireSearch),
    ('GET', '/documents/{workflow_id}/artifacts/{artifact_id}/content', RequireSearch),
    ('GET', '/documents/{workflow_id}/jobs', RequireSearch),
    ('GET', '/documents/{workflow_id}/stage-io', RequireSearch),
    ('GET', '/documents/{workflow_id}/allowed-actions', RequireSearch),
    ('GET', '/documents/{workflow_id}/graph', RequireSearch),
    ('DELETE', '/documents/{workflow_id}', RequireAdmin),
    ('POST', '/documents/{workflow_id}/purge-artifacts', RequireAdmin),
    ('POST', '/documents/{workflow_id}/restore', RequireAdmin),
    ('PATCH', '/documents/{workflow_id}/metadata', RequireReview),
    ('POST', '/documents/{workflow_id}/query-enabled', RequireAdmin),
    ('POST', '/documents/{workflow_id}/reingest', RequirePipeline),
    ('POST', '/documents/{workflow_id}/retry-ingestion', RequirePipeline),
    ('POST', '/documents/{workflow_id}/retry-ocr', RequirePipeline),
    ('POST', '/documents/{workflow_id}/retry-translation', RequirePipeline),
    ('POST', '/documents/{workflow_id}/retry-chunking', RequirePipeline),
    ('POST', '/documents/{workflow_id}/mark-reindex-required', RequirePipeline),
    ('POST', '/documents/{workflow_id}/clear-reindex-required', RequirePipeline),
    ('POST', '/documents/{workflow_id}/demo', RequireAdmin),
    ('POST', '/documents/{workflow_id}/reconcile', RequirePipeline),
    ('POST', '/documents/bulk/approve-ocr', RequireReview),
    ('POST', '/documents/bulk/approve-translation', RequireReview),
    ('POST', '/documents/bulk/approve-chunks', RequireReview),
    ('POST', '/documents/bulk/reindex', RequirePipeline),
    ('POST', '/documents/bulk/auto-tag', RequireReview),
    ('POST', '/documents/{workflow_id}/approve-ocr', RequireReview),
    ('POST', '/documents/{workflow_id}/approve-chunks', RequireReview),
    ('POST', '/documents/{workflow_id}/approve-translation', RequireReview),
    ('POST', '/documents/{workflow_id}/approve-ingestion', RequireReview),
    ('GET', '/audit', RequireSearch),
    ('GET', '/documents/{workflow_id}/audit', RequireSearch),
    ('GET', '/documents/{workflow_id}/pages', RequireSearch),
    ('GET', '/documents/{workflow_id}/pages/{page_num}', RequireSearch),
    ('PATCH', '/documents/{workflow_id}/pages/{page_num}', RequireReview),
    ('POST', '/documents/{workflow_id}/pages/{page_num}/reset', RequireReview),
    ('GET', '/chunks/search', RequireSearch),
    ('GET', '/documents/{workflow_id}/chunks', RequireSearch),
    ('GET', '/documents/{workflow_id}/chunks/{chunk_num}', RequireSearch),
    ('PATCH', '/documents/{workflow_id}/chunks/{chunk_num}', RequireReview),
    ('DELETE', '/documents/{workflow_id}/chunks/{chunk_num}', RequireAdmin),
    ('PUT', '/documents/{workflow_id}/chunks/{chunk_num}/tags', RequireReview),
    ('POST', '/documents/{workflow_id}/auto-tag-chunks', RequireReview),
    ('GET', '/taxonomy/domain-tags', RequireSearch),
    ('POST', '/documents/{workflow_id}/chunks/{chunk_num}/reset', RequireReview),
    ('GET', '/documents/{workflow_id}/export/markdown', RequireSearch),
    ('GET', '/documents/{workflow_id}/export/chunks', RequireSearch),
    ('GET', '/documents/{workflow_id}/pdf', RequireSearch),
    ('GET', '/provenance/chunk', RequireSearch),
    ('GET', '/documents/{workflow_id}/marqo', RequireSearch),
    ('GET', '/documents/{workflow_id}/marqo/chunks', RequireSearch),
    ('GET', '/marqo/indexes/{index_name}/settings', RequireSearch),
    ('GET', '/marqo/indexes/{index_name}/stats', RequireSearch),
    ('GET', '/marqo/indexes/summary', RequireSearch),
    ('POST', '/marqo/search', RequireSearch),
    ('GET', '/admin/index/schema', RequirePlatformAdmin),
    ('POST', '/admin/index/create', RequirePlatformAdmin),
    ('GET', '/tenants', RequirePlatformAdmin),
    ('POST', '/tenants/reconcile', RequirePlatformAdmin),
    ('POST', '/tenants', RequirePlatformAdmin),
    ('POST', '/tenants/{instance}/suspend', RequirePlatformAdmin),
    ('DELETE', '/tenants/{instance}', RequirePlatformAdmin),
    ('GET', '/admin/ingest-info', RequireAdmin),
    ('POST', '/documents/reconcile', RequirePipeline),
    ('GET', '/pipeline/stages', RequireSearch),
    ('GET', '/settings/search', RequireSearch),
    ('PUT', '/settings/search', RequirePlatformAdmin),
    ('GET', '/settings/search/audit', RequirePlatformAdmin),
    ('POST', '/settings/search/reset', RequirePlatformAdmin),
]

_TIER_IDS = [f"{m}:{p}" for m, p, _ in ROUTE_TIERS]


def _dependency_of(annotation: Any):
    """The callable behind an ``Annotated[AuthUser, Depends(...)]`` alias."""
    return annotation.__metadata__[0].dependency


def _expected_detail(tier: Any) -> str:
    perm = TIERS[tier][1]
    return f"Missing permission: {perm.value}" if perm else "Platform admin (master_admin) required"


def _concrete(path: str) -> str:
    for key, value in PATH_PARAMS.items():
        path = path.replace("{" + key + "}", value)
    assert "{" not in path, f"unsubstituted path parameter in {path}"
    return path


def _principal(short_of: Any = None) -> AuthUser:
    """A real (non-bypass) caller: every permission plus ``master_admin``, or, ``short_of``
    a tier, everything but what it requires. For the platform tier that is all the data
    permissions without ``master_admin``: exactly a tenant admin."""
    return AuthUser(
        user_id="guard-user", username="guard-user", email="guard@example.test", roles=["admin"],
        realm_roles=[] if short_of else ["master_admin"],
        permissions=set(Permission) - {TIERS[short_of][1] if short_of else None},
        instances=[PATH_PARAMS["instance"]], envs=["dev"], token_disabled_mode=False,
    )


@pytest.fixture
def auth_on(monkeypatch, tmp_path):
    """A TestClient with auth ENABLED; bearer tokens are opaque handles into a registry of AuthUsers."""
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("KEYCLOAK_ISSUER", "https://keycloak.invalid/realms/docs-pipeline")
    monkeypatch.setenv(
        "KEYCLOAK_JWKS_URL", "https://keycloak.invalid/realms/docs-pipeline/protocol/openid-connect/certs"
    )
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "route_contract.db"))
    monkeypatch.setattr(tenants, "reconcile_tenants", lambda *a, **k: [])  # startup self-heal calls Keycloak
    tokens: dict[str, AuthUser] = {}

    def _fake_decode(token: str, config) -> AuthUser:
        if token not in tokens:
            raise HTTPException(401, "Invalid token")
        return tokens[token]

    monkeypatch.setattr(auth_deps, "decode_and_validate_token", _fake_decode)
    with TestClient(app, raise_server_exceptions=False) as client:
        def request(method: str, path: str, user: AuthUser):
            token = f"guard-token-{len(tokens)}"
            tokens[token] = user
            body = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
            return client.request(method, path, headers={"Authorization": f"Bearer {token}"}, **body)

        yield client, request


@pytest.mark.parametrize("method,path,tier", ROUTE_TIERS, ids=_TIER_IDS)
def test_route_declares_expected_permission_dependency(method: str, path: str, tier: Any):
    """Static pin: catches a downgrade to ``CurrentUser`` or a tier swap, even where a live request is impractical."""
    calls = dependency_calls(app_route(method, path))
    present = sorted(name for alias, (name, _) in TIERS.items() if _dependency_of(alias) in calls)
    assert _dependency_of(tier) in calls, (
        f"{method} {path} no longer depends on {TIERS[tier][0]}; tiers on the route: {present or ['<none>']}"
    )
    assert _dependency_of(CurrentUser) in calls


@pytest.mark.parametrize("method,path,tier", ROUTE_TIERS, ids=_TIER_IDS)
def test_lesser_permission_is_rejected_through_the_app(auth_on, method: str, path: str, tier: Any):
    """A caller one permission short gets 403 with THIS tier's detail (not some in-handler 403)."""
    _, request = auth_on
    response = request(method, _concrete(path), _principal(short_of=tier))
    assert response.status_code == 403, (
        f"{method} {path}: expected 403 from {TIERS[tier][0]}, got {response.status_code} {response.text[:200]}"
    )
    assert response.json().get("detail") == _expected_detail(tier), (
        f"{method} {path}: 403 came from somewhere other than the expected tier: {response.text[:200]}"
    )


@pytest.mark.parametrize("method,path,tier", ROUTE_TIERS, ids=_TIER_IDS)
def test_sufficient_permission_passes_the_tier(auth_on, method: str, path: str, tier: Any):
    """Without this, a route that 403s unconditionally would satisfy the rejection test.
    The handler may still fail on the missing resources; only the tier's own 403 is ruled out."""
    _, request = auth_on
    response = request(method, _concrete(path), _principal())
    if response.status_code == 403:
        assert response.json().get("detail") != _expected_detail(tier), (
            f"{method} {path}: caller holding {TIERS[tier][0]} was still rejected by it"
        )


def test_missing_bearer_token_is_401_when_auth_enabled(auth_on):
    """Proves the fixture really turns auth on, so the tiers above are not reached through the bypass."""
    client, _ = auth_on
    assert client.get("/pipeline/stages").status_code == 401
