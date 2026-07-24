"""Tests for the Keycloak Admin client + tenant user/member provisioning routes.

The HTTP layer is mocked by monkeypatching ``keycloak_admin._http_request`` with a
small stateful in-memory fake Keycloak. Assertions cover:

* create-tenant calls ensure_organization + ensure_group_tree (and degrades
  gracefully when KC admin is unconfigured);
* create-admin posts a user + group membership + a temporary password and returns
  the password;
* unconfigured client secret -> 503 on the user/member routes;
* the RequirePlatformAdmin gate rejects non-platform admins with 403.
"""

from __future__ import annotations

import asyncio
import re

import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock

import pipeline.api as api
import pipeline.db as db_mod
import pipeline.keycloak_admin as kc
from pipeline.auth.deps import require_platform_admin
from pipeline.auth.jwt import claims_to_user


def _run(coro):
    return asyncio.run(coro)


def _master_admin():
    return claims_to_user({"sub": "root", "realm_access": {"roles": ["master_admin"]}})


def _tenant_admin_in(instance: str):
    return claims_to_user({"sub": "tadmin", "tenant_roles": {instance: ["admin"]}})


# ---------------------------------------------------------------------------
# In-memory fake Keycloak Admin REST server (replaces _http_request)
# ---------------------------------------------------------------------------


class FakeKeycloak:
    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.orgs: list[dict] = []
        self.groups: dict[str, dict] = {}  # id -> {name, parent, id}
        self.users: dict[str, dict] = {}  # id -> representation
        self.memberships: dict[str, set[str]] = {}  # group_id -> {user_id}
        self.org_members: dict[str, set[str]] = {}  # org_id -> {user_id}
        self.passwords: dict[str, dict] = {}  # user_id -> credential
        self._seq = 0

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    # The callable installed in place of keycloak_admin._http_request.
    def __call__(self, method, url, *, token=None, body=None, form=None, timeout=30):
        self.calls.append((method, url, body if body is not None else form))

        if "openid-connect/token" in url:
            return 200, {"access_token": "fake-admin-token", "expires_in": 300}

        base = kc._admin_base_url()
        assert url.startswith(base), f"unexpected url {url}"
        path = url[len(base):]
        return self._route(method, path, body)

    def _route(self, method, path, body):
        # Organizations
        if path == "/organizations" and method == "GET":
            return 200, list(self.orgs)
        if path == "/organizations" and method == "POST":
            org = {"id": self._new_id("org"), "name": body["name"], "alias": body.get("alias")}
            self.orgs.append(org)
            return 201, None
        m = re.fullmatch(r"/organizations/([^/]+)/members", path)
        if m and method == "POST":
            self.org_members.setdefault(m.group(1), set()).add(body)
            return 204, None

        # Top-level groups
        if path.startswith("/groups?") and method == "GET":
            search = _query_param(path, "search")
            hits = [
                g for g in self.groups.values()
                if g["parent"] is None and (search is None or search in g["name"])
            ]
            return 200, hits
        if path == "/groups" and method == "POST":
            gid = self._new_id("grp")
            self.groups[gid] = {"id": gid, "name": body["name"], "parent": None}
            return 201, None

        # Children
        m = re.fullmatch(r"/groups/([^/]+)/children", path)
        if m and method == "GET":
            pid = m.group(1)
            return 200, [g for g in self.groups.values() if g["parent"] == pid]
        if m and method == "POST":
            pid = m.group(1)
            gid = self._new_id("grp")
            self.groups[gid] = {"id": gid, "name": body["name"], "parent": pid}
            return 201, None

        # Group members
        m = re.fullmatch(r"/groups/([^/]+)/members", path)
        if m and method == "GET":
            gid = m.group(1)
            return 200, [self.users[uid] for uid in self.memberships.get(gid, set())]

        # Users
        if path.startswith("/users?") and method == "GET":
            uname = _query_param(path, "username")
            return 200, [u for u in self.users.values() if u["username"] == uname]
        if path == "/users" and method == "POST":
            uid = self._new_id("usr")
            self.users[uid] = {"id": uid, **body}
            return 201, None
        m = re.fullmatch(r"/users/([^/]+)", path)
        if m and method == "PUT":
            uid = m.group(1)
            self.users[uid] = {"id": uid, **body}
            return 204, None
        m = re.fullmatch(r"/users/([^/]+)/reset-password", path)
        if m and method == "PUT":
            self.passwords[m.group(1)] = body
            return 204, None
        m = re.fullmatch(r"/users/([^/]+)/groups/([^/]+)", path)
        if m and method == "PUT":
            self.memberships.setdefault(m.group(2), set()).add(m.group(1))
            return 204, None

        raise AssertionError(f"unhandled fake KC route: {method} {path}")

    # test conveniences ------------------------------------------------------
    def called(self, method, pattern) -> bool:
        rx = re.compile(pattern)
        return any(m == method and rx.search(u) for m, u, _ in self.calls)


def _query_param(path: str, key: str):
    import urllib.parse
    query = path.split("?", 1)[1] if "?" in path else ""
    values = urllib.parse.parse_qs(query).get(key)
    return values[0] if values else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kc_configured(monkeypatch):
    """Configure KC admin + install the fake HTTP layer. Yields the fake."""
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "docs-pipeline-admin")
    monkeypatch.setenv("KEYCLOAK_ISSUER", "https://sso.example.com/auth/realms/docs-pipeline")
    monkeypatch.delenv("KEYCLOAK_ADMIN_BASE_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    kc.reset_token_cache()
    fake = FakeKeycloak()
    monkeypatch.setattr(kc, "_http_request", fake)
    yield fake
    kc.reset_token_cache()


def _patch_marqo(monkeypatch):
    monkeypatch.setattr(api, "db", db_mod)
    monkeypatch.setattr(api, "_create_marqo_index_with_schema", MagicMock(return_value={}))
    monkeypatch.setattr(api, "_marqo_client", lambda: MagicMock())


# ---------------------------------------------------------------------------
# keycloak_admin module unit tests
# ---------------------------------------------------------------------------


def test_token_endpoints_admin_host_first_then_issuer(monkeypatch):
    # The service-account token must be minted from the SAME host as the Admin
    # API (KEYCLOAK_ADMIN_BASE_URL) so `iss` matches and KC doesn't 401. The
    # public issuer is only a fallback.
    monkeypatch.setenv("KEYCLOAK_ADMIN_BASE_URL", "http://keycloak:8080/auth")
    monkeypatch.setenv("KEYCLOAK_ISSUER", "https://sso.example.com/auth/realms/docs-pipeline")
    monkeypatch.setenv(
        "KEYCLOAK_JWKS_URL",
        "http://keycloak:8080/auth/realms/docs-pipeline/protocol/openid-connect/certs",
    )
    endpoints = kc._token_endpoints()
    # admin-base host first (issuer-consistent with the Admin API host)
    assert endpoints[0] == "http://keycloak:8080/auth/realms/docs-pipeline/protocol/openid-connect/token"
    # public issuer is a fallback candidate
    assert "https://sso.example.com/auth/realms/docs-pipeline/protocol/openid-connect/token" in endpoints
    # no duplicate (JWKS-derived == admin-base here)
    assert len(endpoints) == len(set(endpoints))


def test_admin_base_url_defaults(monkeypatch):
    monkeypatch.delenv("KEYCLOAK_ADMIN_BASE_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    assert kc._admin_base_url() == "http://keycloak:8080/auth/admin/realms/docs-pipeline"


def test_unconfigured_secret_raises(monkeypatch):
    monkeypatch.delenv("KEYCLOAK_ADMIN_CLIENT_SECRET", raising=False)
    kc.reset_token_cache()
    assert kc.is_configured() is False
    with pytest.raises(kc.KeycloakAdminUnconfigured):
        kc._admin_token()
    with pytest.raises(kc.KeycloakAdminUnconfigured):
        kc.list_members("tenant-x")


def test_placeholder_secret_is_treated_as_unconfigured(monkeypatch):
    """C1: the well-known placeholder secret must NEVER authenticate — it counts as
    unconfigured so the routes 503 instead of using a guessable credential."""
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", kc.PLACEHOLDER_ADMIN_SECRET)
    kc.reset_token_cache()
    assert kc.is_configured() is False
    with pytest.raises(kc.KeycloakAdminUnconfigured):
        kc._admin_token()
    with pytest.raises(kc.KeycloakAdminUnconfigured):
        kc.list_members("tenant-x")


def test_find_user_by_username_requires_exact_match(monkeypatch):
    """L5: never return a fuzzy KC result — only an exact (case-insensitive) match."""
    monkeypatch.setattr(kc, "_admin_call", lambda *a, **k: (200, [{"id": "x", "username": "alice-2"}]))
    assert kc._find_user_by_username("alice") is None
    monkeypatch.setattr(kc, "_admin_call", lambda *a, **k: (200, [{"id": "y", "username": "Alice"}]))
    assert kc._find_user_by_username("alice")["id"] == "y"


def test_create_user_existing_is_merge_only(kc_configured):
    """H3: an existing username is merged (group added) — rep/password untouched, no
    temporary_password returned."""
    fake = kc_configured
    kc.ensure_group_tree("tenant-x")
    # Seed a pre-existing user with custom attributes + a known (non-temp) password.
    uid = fake._new_id("usr")
    fake.users[uid] = {
        "id": uid,
        "username": "alice",
        "email": "real@corp.example",
        "attributes": {"instances": ["tenant-x"], "custom": ["keep-me"]},
    }
    fake.passwords[uid] = {"type": "password", "temporary": False, "value": "original-pw"}

    out = kc.create_user("alice", None, "New-Temp-Pass-1!", "/tenant-x/viewer")
    assert out["created"] is False
    assert "temporary_password" not in out
    assert out["id"] == uid
    # Representation + password left intact (no PUT-replace, no reset-password).
    assert fake.users[uid]["email"] == "real@corp.example"
    assert fake.users[uid]["attributes"]["custom"] == ["keep-me"]
    assert fake.passwords[uid]["value"] == "original-pw"
    # But the requested group membership WAS added.
    gid = kc._resolve_group_tree("tenant-x")["/tenant-x/viewer"]
    assert uid in fake.memberships[gid]


def test_ensure_group_tree_creates_children(kc_configured):
    fake = kc_configured
    ids = kc.ensure_group_tree("tenant-x")
    assert set(ids.keys()) == {"/tenant-x", "/tenant-x/admin", "/tenant-x/content_curator", "/tenant-x/viewer"}
    # Idempotent: a second call creates nothing new.
    posts_before = sum(1 for m, u, _ in fake.calls if m == "POST" and u.endswith("/groups"))
    kc.ensure_group_tree("tenant-x")
    posts_after = sum(1 for m, u, _ in fake.calls if m == "POST" and u.endswith("/groups"))
    assert posts_after == posts_before  # top group reused


def test_create_user_sets_password_and_membership(kc_configured):
    fake = kc_configured
    kc.ensure_group_tree("tenant-x")
    out = kc.create_user(
        username="alice",
        email=None,
        temporary_password="Temp-Pass-123!",
        group_path="/tenant-x/admin",
    )
    uid = out["id"]
    # firstName/lastName present (KC26 requirement).
    assert fake.users[uid]["firstName"]
    assert fake.users[uid]["lastName"]
    assert fake.users[uid]["emailVerified"] is True
    # Password credential written as temporary.
    assert fake.passwords[uid]["temporary"] is True
    assert fake.passwords[uid]["value"] == "Temp-Pass-123!"
    # Joined the /tenant-x/admin group.
    admin_gid = kc._resolve_group_tree("tenant-x")["/tenant-x/admin"]
    assert uid in fake.memberships[admin_gid]


def test_list_members_reports_roles(kc_configured):
    kc.ensure_group_tree("tenant-x")
    kc.create_user("alice", None, "Temp-Pass-123!", "/tenant-x/admin")
    kc.create_user("bob", "bob@x.example.com", "Temp-Pass-456!", "/tenant-x/viewer")
    members = kc.list_members("tenant-x")
    by_name = {m["username"]: m for m in members}
    assert by_name["alice"]["roles"] == ["admin"]
    assert by_name["bob"]["roles"] == ["viewer"]
    assert by_name["bob"]["email"] == "bob@x.example.com"


def test_generate_temporary_password_is_strong():
    pwd = kc.generate_temporary_password()
    assert len(pwd) >= 16
    assert any(c.islower() for c in pwd)
    assert any(c.isupper() for c in pwd)
    assert any(c.isdigit() for c in pwd)


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


def test_create_tenant_calls_org_and_group_tree(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    fake = kc_configured
    out = _run(api.create_tenant_route({"instance": "tenant-x", "display_name": "Tenant X"}, _master_admin()))
    assert out["tenant"]["id"] == "tenant-x"
    # Organization was created + group tree provisioned.
    assert fake.called("POST", r"/organizations$")
    assert set(out["keycloak"]["groups"]) == {
        "/tenant-x", "/tenant-x/admin", "/tenant-x/content_curator", "/tenant-x/viewer"
    }
    assert "warning" not in out


def test_create_tenant_graceful_when_unconfigured(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    monkeypatch.delenv("KEYCLOAK_ADMIN_CLIENT_SECRET", raising=False)
    kc.reset_token_cache()
    out = _run(api.create_tenant_route({"instance": "tenant-y"}, _master_admin()))
    # App-side tenant still created; identity plane skipped with a warning.
    assert out["tenant"]["id"] == "tenant-y"
    assert out["keycloak"] is None
    assert "warning" in out
    assert db_mod.get_tenant("tenant-y") is not None


def test_create_admin_returns_temp_password(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    fake = kc_configured
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    out = _run(api.create_tenant_admin_route("tenant-x", {"username": "alice"}, _master_admin()))
    assert out["username"] == "alice"
    assert out["temporary_password"]
    # A user was posted, a temp password set, and admin-group membership added.
    assert fake.called("POST", r"/users$")
    assert fake.called("PUT", r"/users/[^/]+/reset-password$")
    assert fake.called("PUT", r"/users/[^/]+/groups/[^/]+$")
    admin_gid = kc._resolve_group_tree("tenant-x")["/tenant-x/admin"]
    assert fake.memberships.get(admin_gid)


def test_create_admin_unknown_tenant_404(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_admin_route("ghost", {"username": "alice"}, _master_admin()))
    assert exc.value.status_code == 404


def test_create_member_bad_role_400(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_member_route("tenant-x", {"username": "x", "role": "superuser"}, _master_admin()))
    assert exc.value.status_code == 400


def test_member_routes_503_when_unconfigured(db_connection, monkeypatch):
    _patch_marqo(monkeypatch)
    monkeypatch.delenv("KEYCLOAK_ADMIN_CLIENT_SECRET", raising=False)
    kc.reset_token_cache()
    # Tenant exists on the app side (created without KC), so we reach the KC call.
    db_mod.create_tenant("tenant-z")
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_admin_route("tenant-z", {"username": "alice"}, _master_admin()))
    assert exc.value.status_code == 503
    with pytest.raises(HTTPException) as exc2:
        _run(api.list_tenant_members_route("tenant-z", _master_admin()))
    assert exc2.value.status_code == 503


def test_member_route_503_when_secret_is_placeholder(db_connection, monkeypatch):
    """C1: a leftover placeholder secret must 503 the member routes (not use it)."""
    _patch_marqo(monkeypatch)
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", kc.PLACEHOLDER_ADMIN_SECRET)
    kc.reset_token_cache()
    db_mod.create_tenant("tenant-z")
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_admin_route("tenant-z", {"username": "alice"}, _master_admin()))
    assert exc.value.status_code == 503


def test_create_admin_existing_user_merges_without_password(db_connection, monkeypatch, kc_configured):
    """H3 (route): re-adding an existing username returns created=False, omits the
    temporary_password, indicates the group it was added to, and never resets the pw."""
    _patch_marqo(monkeypatch)
    fake = kc_configured
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    first = _run(api.create_tenant_admin_route("tenant-x", {"username": "alice"}, _master_admin()))
    assert first["created"] is True
    assert first["temporary_password"]
    uid = first["user_id"]
    pw_before = dict(fake.passwords[uid])
    attrs_before = dict(fake.users[uid].get("attributes") or {})

    # Add the same user to a different role group — must merge, not hijack.
    second = _run(api.create_tenant_member_route(
        "tenant-x", {"username": "alice", "role": "viewer"}, _master_admin()
    ))
    assert second["created"] is False
    assert "temporary_password" not in second
    assert second["added_to_group"] == "/tenant-x/viewer"
    # Password + attributes untouched; the viewer group membership was added.
    assert fake.passwords[uid] == pw_before
    assert (fake.users[uid].get("attributes") or {}) == attrs_before
    viewer_gid = kc._resolve_group_tree("tenant-x")["/tenant-x/viewer"]
    assert uid in fake.memberships[viewer_gid]


def test_list_members_route(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    _run(api.create_tenant_admin_route("tenant-x", {"username": "alice"}, _master_admin()))
    members = _run(api.list_tenant_members_route("tenant-x", _master_admin()))
    assert any(m["username"] == "alice" and "admin" in m["roles"] for m in members)


def test_platform_admin_gate_rejects_tenant_admin():
    # RequirePlatformAdmin must reject a per-tenant admin (403).
    with pytest.raises(HTTPException) as exc:
        _run(require_platform_admin(_tenant_admin_in("tenant-x")))
    assert exc.value.status_code == 403
    # A real platform (master) admin passes.
    assert _run(require_platform_admin(_master_admin())) is not None
