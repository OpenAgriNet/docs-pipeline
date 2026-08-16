"""Route contract guards — the safety net the APIRouter split must ride on.

Two invariants of the assembled FastAPI application are otherwise unguarded, and breaking
either fails **silently** (no import error, no 500 — just a wrong handler or a
missing authorization check):

**Guard A — route ordering.** Starlette matches routes in *registration order*
and the first full match wins. Every ``{workflow_id}`` / ``{instance}`` path
parameter is an unconstrained ``str``, so ``/documents/{workflow_id}/approve-ocr``
happily swallows the literal ``/documents/bulk/approve-ocr``. Today the literal
routes only win because they are declared earlier in the file. Splitting the 97
routes into APIRouter modules re-orders registration; if the parameterized
router is included first, bulk approvals silently start operating on a document
whose ``workflow_id`` is the literal string ``"bulk"``. The tests below assert
**actual resolution** (Starlette's own matcher, the same first-``Match.FULL``
walk ``starlette.routing.Router`` performs) rather than comparing indices in
``app.routes``.

**Guard B — permission dependencies actually execute.** ``test_tenant_isolation``
calls handlers as plain Python functions and therefore never runs FastAPI's
dependency graph, so the ``RequireReview`` / ``RequirePipeline`` / ``RequireAdmin``
annotations on ~50 routes were asserted by nothing: every one could be
downgraded to ``CurrentUser`` and the suite would stay green.

*The AUTH_DISABLED obstacle.* ``tests/conftest.py`` sets ``AUTH_DISABLED=true``
process-wide and ``pipeline/auth/deps.get_current_user`` re-reads that config on
**every request**, so a plain ``TestClient`` call always gets
``local_bypass_user()`` — which holds every permission and can never be
rejected. These tests therefore stand up their own client with
``AUTH_DISABLED=false`` (monkeypatched, so the global default is untouched for
every other test) plus a stubbed ``decode_and_validate_token`` that maps an
opaque bearer token to a chosen ``AuthUser``. Auth is genuinely **on**: the
config branch, the ``Bearer`` header parse, and the real ``require_permission``
/ ``require_platform_admin`` dependencies all execute. Only the Keycloak
signature check (which would need a live JWKS endpoint) is stubbed.
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
from pipeline.auth.models import AuthUser, local_bypass_user
from pipeline.auth.permissions import Permission
from pipeline.services import tenants


# =============================================================================
# Guard A — literal routes must resolve ahead of their parameterized twins
# =============================================================================


def _resolve(method: str, path: str):
    """Return the route Starlette would dispatch to, via its own matcher.

    Mirrors ``starlette.routing.Router.app``: walk ``app.routes`` in
    registration order and take the first ``Match.FULL``. Asserting on this is
    strictly stronger than comparing indices in ``app.routes`` — an index
    comparison can pass while real matching is broken (e.g. a path-parameter
    converter change).
    """
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }
    for route in app.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route
    return None


def _route_for(method: str, path: str):
    """The registered route object with exactly this path template + method."""
    for route in app.routes:
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", None) or set()):
            return route
    raise AssertionError(f"route not registered: {method} {path}")


# (method, literal_path, expected_handler_name)
#
# Every literal path that sits under a prefix which also has parameterized
# routes. Rows marked "no twin today" have no same-method, same-arity
# parameterized route right now — they are pinned anyway so that adding one
# later (e.g. ``POST /documents/{workflow_id}``) trips this test instead of
# silently hijacking the literal.
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


# (method, literal_path, literal_handler, parameterized_path, parameterized_handler)
#
# Pairs where the parameterized path template *does* match the literal path, so
# ordering is the only thing keeping them apart.
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


@pytest.mark.parametrize(
    "method,path,expected_handler",
    LITERAL_ROUTES,
    ids=[f"{m}:{p}" for m, p, _ in LITERAL_ROUTES],
)
def test_literal_route_resolves_to_its_own_handler(method: str, path: str, expected_handler: str):
    """A literal path must dispatch to the literal handler, not a param route."""
    route = _resolve(method, path)
    assert route is not None, f"nothing matched {method} {path}"
    actual = route.endpoint.__name__
    assert actual == expected_handler, (
        f"{method} {path} resolved to {actual!r} (template {route.path!r}) "
        f"instead of {expected_handler!r} — a parameterized route was registered first"
    )


@pytest.mark.parametrize(
    "method,literal,literal_handler,param,param_handler",
    COLLIDING_PAIRS,
    ids=[f"{m}:{lit}" for m, lit, _, _, _ in COLLIDING_PAIRS],
)
def test_literal_route_wins_over_parameterized_twin(
    method: str, literal: str, literal_handler: str, param: str, param_handler: str
):
    """The pair is genuinely ambiguous, and the literal must win.

    Asserted in two steps so the test cannot pass vacuously: first that the
    parameterized template really does match the literal path (if it stopped
    matching — e.g. someone constrained the converter — this row is obsolete and
    should be moved to LITERAL_ROUTES), then that resolution lands on the
    literal handler.
    """
    param_route = _route_for(method, param)
    assert param_route.endpoint.__name__ == param_handler
    assert param_route.path_regex.match(literal), (
        f"{param} no longer matches {literal}; this collision row is stale"
    )

    resolved = _resolve(method, literal)
    assert resolved is not None
    assert resolved.endpoint.__name__ == literal_handler, (
        f"{method} {literal} was swallowed by {param} -> {resolved.endpoint.__name__}"
    )


def test_no_unlisted_literal_parameterized_collisions():
    """Every literal/parameterized collision in the app is covered above.

    Keeps the tables honest: a new literal route added under an existing
    parameterized prefix fails here until it is pinned in COLLIDING_PAIRS.
    """
    routes = [
        r for r in app.routes
        if getattr(r, "path_regex", None) is not None and getattr(r, "methods", None)
    ]
    literals = [r for r in routes if "{" not in r.path]
    params = [r for r in routes if "{" in r.path]

    found: set[tuple[str, str, str]] = set()
    for lit in literals:
        for method in lit.methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            for par in params:
                if method not in par.methods:
                    continue
                if par.path_regex.match(lit.path):
                    found.add((method, lit.path, par.path))

    pinned = {(m, lit, par) for m, lit, _, par, _ in COLLIDING_PAIRS}
    assert found == pinned, (
        f"unpinned collisions: {sorted(found - pinned)}; stale rows: {sorted(pinned - found)}"
    )


# =============================================================================
# Guard B — required permission tier per route, exercised through the app
# =============================================================================


def _dependency_of(annotation: Any):
    """The callable behind a ``Annotated[AuthUser, Depends(...)]`` alias."""
    return annotation.__metadata__[0].dependency


# tier alias -> (permission it requires, the 403 detail it produces)
TIER_PERMISSION: dict[Any, Permission | None] = {
    RequireUpload: Permission.UPLOAD,
    RequireReview: Permission.REVIEW,
    RequirePipeline: Permission.PIPELINE,
    RequireSearch: Permission.SEARCH,
    RequireAdmin: Permission.ADMIN,
    RequirePlatformAdmin: None,  # control-plane gate, not a data permission
}

_TIER_NAMES: dict[Any, str] = {
    RequireUpload: "RequireUpload",
    RequireReview: "RequireReview",
    RequirePipeline: "RequirePipeline",
    RequireSearch: "RequireSearch",
    RequireAdmin: "RequireAdmin",
    RequirePlatformAdmin: "RequirePlatformAdmin",
}

_PLATFORM_ADMIN_DETAIL = "Platform admin (master_admin) required"


def _expected_detail(tier: Any) -> str:
    perm = TIER_PERMISSION[tier]
    if perm is None:
        return _PLATFORM_ADMIN_DETAIL
    return f"Missing permission: {perm.value}"


# Concrete values for path parameters when issuing the live request. The
# resources deliberately do not exist — a permission failure must be raised
# during dependency resolution, before the handler ever looks anything up.
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


def _concrete(path: str) -> str:
    for key, value in PATH_PARAMS.items():
        path = path.replace("{" + key + "}", value)
    assert "{" not in path, f"unsubstituted path parameter in {path}"
    return path


# ---------------------------------------------------------------------------
# THE PIN: (method, path template, required tier).
#
# Every route in the assembled app that carries a permission tier. Routes
# annotated with bare ``CurrentUser`` are intentionally absent — they gate
# in-handler with instance-aware checks (``_assert_can_view_tenant`` etc.) and
# are covered by tests/test_tenant_isolation.py.
# ---------------------------------------------------------------------------
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


def _principal(permissions, *, platform_admin: bool = False) -> AuthUser:
    """A real (non-bypass) principal with an explicit permission set."""
    return AuthUser(
        user_id="guard-user",
        username="guard-user",
        email="guard@example.test",
        roles=["admin"],
        realm_roles=["master_admin"] if platform_admin else [],
        permissions=set(permissions),
        instances=[PATH_PARAMS["instance"]],
        envs=["dev"],
        token_disabled_mode=False,
    )


def _lesser_principal(tier: Any) -> AuthUser:
    """A caller holding every permission *except* the one this tier requires.

    For the platform-admin tier that means a caller with all six data
    permissions but no realm ``master_admin`` role — precisely the per-tenant
    admin that must not reach the control plane.
    """
    perm = TIER_PERMISSION[tier]
    if perm is None:
        return _principal(set(Permission), platform_admin=False)
    return _principal(set(Permission) - {perm}, platform_admin=False)


def _sufficient_principal() -> AuthUser:
    return _principal(set(Permission), platform_admin=True)


@pytest.fixture
def auth_on(monkeypatch, tmp_path):
    """A TestClient with auth genuinely ENABLED.

    ``conftest.py`` sets ``AUTH_DISABLED=true`` for the whole session and
    ``get_current_user`` re-reads it per request, so without this fixture every
    call is ``local_bypass_user()`` and no permission tier can ever reject.
    Flipping it here (monkeypatched, restored after the test) makes
    ``get_current_user`` take the real branch: parse ``Authorization: Bearer``,
    then validate. Only the Keycloak/JWKS signature check is stubbed — tokens
    are opaque handles into a registry of AuthUsers.
    """
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("KEYCLOAK_ISSUER", "https://keycloak.invalid/realms/docs-pipeline")
    monkeypatch.setenv(
        "KEYCLOAK_JWKS_URL",
        "https://keycloak.invalid/realms/docs-pipeline/protocol/openid-connect/certs",
    )
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "route_contract.db"))
    # Startup self-heal talks to Keycloak; never needed for a permission check.
    monkeypatch.setattr(tenants, "reconcile_tenants", lambda *a, **k: [])

    tokens: dict[str, AuthUser] = {}

    def _fake_decode(token: str, config) -> AuthUser:
        try:
            return tokens[token]
        except KeyError:
            raise HTTPException(401, "Invalid token")

    monkeypatch.setattr(auth_deps, "decode_and_validate_token", _fake_decode)

    with TestClient(app, raise_server_exceptions=False) as client:
        def headers_for(user: AuthUser) -> dict[str, str]:
            token = f"guard-token-{len(tokens)}"
            tokens[token] = user
            return {"Authorization": f"Bearer {token}"}

        yield client, headers_for


def _request(client: TestClient, method: str, path: str, headers: dict[str, str]):
    kwargs: dict[str, Any] = {"headers": headers}
    if method in ("POST", "PUT", "PATCH"):
        kwargs["json"] = {}
    return client.request(method, path, **kwargs)


@pytest.mark.parametrize("method,path,tier", ROUTE_TIERS, ids=_TIER_IDS)
def test_route_declares_expected_permission_dependency(method: str, path: str, tier: Any):
    """Static pin: the expected tier callable is in the route's dependant graph.

    Catches a downgrade to ``CurrentUser`` (or a swap to a different tier) even
    for routes where a live request is impractical.
    """
    route = _route_for(method, path)
    calls: set = set()
    stack = [route.dependant]
    while stack:
        dep = stack.pop()
        calls.add(dep.call)
        stack.extend(dep.dependencies)

    expected = _dependency_of(tier)
    present = sorted(
        _TIER_NAMES[alias] for alias in TIER_PERMISSION if _dependency_of(alias) in calls
    )
    assert expected in calls, (
        f"{method} {path} no longer depends on the expected permission tier "
        f"{_TIER_NAMES[tier]}; tiers actually on the route: {present or ['<none>']}"
    )
    # A bare CurrentUser downgrade leaves only get_current_user behind.
    assert _dependency_of(CurrentUser) in calls


@pytest.mark.parametrize("method,path,tier", ROUTE_TIERS, ids=_TIER_IDS)
def test_lesser_permission_is_rejected_through_the_app(
    auth_on, method: str, path: str, tier: Any
):
    """Behavioural pin: a caller one permission short gets 403 from that tier.

    Runs through the real ASGI stack with auth on, so the ``RequireX``
    dependency actually executes. The detail string is asserted too — a 403
    raised by some other in-handler check would not satisfy this.
    """
    client, headers_for = auth_on
    response = _request(client, method, _concrete(path), headers_for(_lesser_principal(tier)))

    assert response.status_code == 403, (
        f"{method} {path}: expected 403 from {_TIER_NAMES[tier]}, "
        f"got {response.status_code} {response.text[:200]}"
    )
    assert response.json().get("detail") == _expected_detail(tier), (
        f"{method} {path}: 403 came from somewhere other than the expected tier: "
        f"{response.text[:200]}"
    )


@pytest.mark.parametrize("method,path,tier", ROUTE_TIERS, ids=_TIER_IDS)
def test_sufficient_permission_passes_the_tier(auth_on, method: str, path: str, tier: Any):
    """Counterpart: a caller holding the tier is not rejected *by that tier*.

    Without this, a route that 403s unconditionally would satisfy the rejection
    test above. The handler itself may still fail (the fixtured resources do not
    exist) — only the tier's own 403 is ruled out.
    """
    client, headers_for = auth_on
    response = _request(client, method, _concrete(path), headers_for(_sufficient_principal()))

    if response.status_code == 403:
        assert response.json().get("detail") != _expected_detail(tier), (
            f"{method} {path}: caller holding {_TIER_NAMES[tier]} was still rejected by it"
        )


def test_missing_bearer_token_is_401_when_auth_enabled(auth_on):
    """Proves the fixture really turns auth on (and that the tiers above are
    not being reached through the AUTH_DISABLED bypass)."""
    client, _ = auth_on
    response = client.get("/pipeline/stages")
    assert response.status_code == 401


def test_conftest_bypass_would_defeat_every_permission_tier():
    """Documents the obstacle these tests work around.

    Under the suite-wide ``AUTH_DISABLED=true``, ``get_current_user`` short
    circuits to ``local_bypass_user()``, which holds every permission — so a
    TestClient request can never exercise a tier. If this ever stops being
    true, the ``auth_on`` fixture can be simplified.
    """
    from pipeline.auth.config import load_auth_config

    assert load_auth_config().disabled is True
    bypass = local_bypass_user()
    for permission in Permission:
        assert bypass.has_permission(permission)
    assert bypass.is_platform_admin is True
