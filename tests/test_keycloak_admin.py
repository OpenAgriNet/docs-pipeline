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


def _viewer_in(instance: str):
    return claims_to_user({"sub": "vwr", "tenant_roles": {instance: ["viewer"]}})


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
        self.realm_roles: dict[str, set[str]] = {}  # user_id -> {DIRECTLY assigned realm role}
        # Composite realm roles: role name -> realm roles that holding it implies.
        self.composite_roles: dict[str, set[str]] = {}
        # Realm roles mapped onto a GROUP: group_id -> {realm role name}. Members of
        # the group (and of its child groups) hold them without any direct mapping.
        self.group_realm_roles: dict[str, set[str]] = {}
        # Group ids whose DELETE must fail (simulates a partial detach).
        self.fail_delete_groups: set[str] = set()
        # Group ids whose PUT (join) must fail (simulates a failed role join).
        self.fail_join_groups: set[str] = set()
        self._seq = 0

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def _group_path(self, gid: str) -> str:
        names = []
        group = self.groups.get(gid)
        while group:
            names.append(group["name"])
            group = self.groups.get(group["parent"]) if group["parent"] else None
        return "/" + "/".join(reversed(names))

    def _effective_realm_roles(self, uid: str) -> set[str]:
        """Every realm role ``uid`` effectively holds, the way Keycloak computes it.

        Seeds from the direct mappings plus the roles mapped onto every group the
        user is in (and that group's ancestors — group role mappings are inherited
        downwards), then closes over the composite graph.
        """
        seed = set(self.realm_roles.get(uid, set()))
        for gid, members in self.memberships.items():
            if uid not in members:
                continue
            group = self.groups.get(gid)
            while group:
                seed |= self.group_realm_roles.get(group["id"], set())
                group = self.groups.get(group["parent"]) if group["parent"] else None
        effective: set[str] = set()
        pending = list(seed)
        while pending:
            role = pending.pop()
            if role in effective:
                continue
            effective.add(role)
            pending.extend(self.composite_roles.get(role, set()))
        return effective

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
        # Effective realm role mappings: direct + composite-derived + group-derived.
        # This is what real Keycloak returns from .../role-mappings/realm/composite,
        # and it is the only view that sees an indirectly held platform-admin role.
        m = re.fullmatch(r"/users/([^/]+)/role-mappings/realm/composite", path)
        if m and method == "GET":
            return 200, [{"name": r} for r in sorted(self._effective_realm_roles(m.group(1)))]
        # DIRECT realm role mappings only — no composites, no group-derived roles.
        m = re.fullmatch(r"/users/([^/]+)/role-mappings/realm", path)
        if m and method == "GET":
            return 200, [{"name": r} for r in sorted(self.realm_roles.get(m.group(1), set()))]
        # Every group the user is in, with its full path (drives the cross-tenant guard).
        m = re.fullmatch(r"/users/([^/]+)/groups", path)
        if m and method == "GET":
            uid = m.group(1)
            return 200, [
                {"id": gid, "path": self._group_path(gid)}
                for gid, members in self.memberships.items()
                if uid in members
            ]
        m = re.fullmatch(r"/users/([^/]+)/groups/([^/]+)", path)
        if m and method == "PUT":
            if m.group(2) in self.fail_join_groups:
                raise kc.KeycloakAdminError(f"PUT {path} -> 500: join refused")
            self.memberships.setdefault(m.group(2), set()).add(m.group(1))
            return 204, None
        if m and method == "DELETE":
            if m.group(2) in self.fail_delete_groups:
                raise kc.KeycloakAdminError(f"DELETE {path} -> 500: detach refused")
            self.memberships.setdefault(m.group(2), set()).discard(m.group(1))
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
    temporary_password returned. The merge is platform-admin-only (``allow_existing``)."""
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

    out = kc.create_user("alice", None, "New-Temp-Pass-1!", "/tenant-x/viewer", allow_existing=True)
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


# ---------------------------------------------------------------------------
# keycloak_admin remove / role-change / reset-password primitives
# ---------------------------------------------------------------------------


def _seed_tenant_member(instance: str, username: str, role: str = "viewer") -> str:
    """Create the tenant (if new) + a member; return the member's user_id."""
    _run(api.create_tenant_route({"instance": instance}, _master_admin()))
    out = _run(api.create_tenant_member_route(
        instance, {"username": username, "role": role}, _master_admin()
    ))
    return out["user_id"]


def test_list_members_includes_user_id(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    uid = _seed_tenant_member("tenant-x", "alice", "admin")
    members = _run(api.list_tenant_members_route("tenant-x", _master_admin()))
    alice = next(m for m in members if m["username"] == "alice")
    assert alice["user_id"] == uid


def test_remove_from_group_detaches_all_roles(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    fake = kc_configured
    uid = _seed_tenant_member("tenant-x", "bob", "viewer")
    viewer_gid = kc._resolve_group_tree("tenant-x")["/tenant-x/viewer"]
    assert uid in fake.memberships[viewer_gid]

    out = kc.remove_from_group("tenant-x", uid)
    assert out["was_member"] is True
    assert out["removed_roles"] == ["viewer"]
    assert uid not in fake.memberships[viewer_gid]


def test_remove_from_group_non_member_reports_false(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    out = kc.remove_from_group("tenant-x", "ghost-uid")
    assert out["was_member"] is False
    assert out["removed_roles"] == []


def test_set_member_role_swaps_to_single_role(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    fake = kc_configured
    uid = _seed_tenant_member("tenant-x", "carol", "viewer")
    tree = kc._resolve_group_tree("tenant-x")

    out = kc.set_member_role("tenant-x", uid, "admin")
    assert out["was_member"] is True
    assert out["role"] == "admin"
    assert out["previous_roles"] == ["viewer"]
    assert uid in fake.memberships[tree["/tenant-x/admin"]]
    assert uid not in fake.memberships[tree["/tenant-x/viewer"]]


def test_set_member_role_non_member_reports_false(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    out = kc.set_member_role("tenant-x", "ghost-uid", "admin")
    assert out["was_member"] is False


def test_set_member_role_rejects_platform_role(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    uid = _seed_tenant_member("tenant-x", "dave", "viewer")
    with pytest.raises(kc.KeycloakAdminError):
        kc.set_member_role("tenant-x", uid, "master_admin")


def test_reset_password_sets_temporary_credential(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    fake = kc_configured
    uid = _seed_tenant_member("tenant-x", "erin", "viewer")

    out = kc.reset_password("tenant-x", uid)
    assert out["was_member"] is True
    assert out["temporary_password"]
    assert fake.passwords[uid]["value"] == out["temporary_password"]
    assert fake.passwords[uid]["temporary"] is True


def test_reset_password_non_member_reports_false(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    out = kc.reset_password("tenant-x", "ghost-uid")
    assert out["was_member"] is False
    assert out["temporary_password"] is None


# ---------------------------------------------------------------------------
# Member-management routes: happy paths
# ---------------------------------------------------------------------------


def test_remove_member_route(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    fake = kc_configured
    uid = _seed_tenant_member("tenant-x", "bob", "viewer")
    out = _run(api.remove_tenant_member_route("tenant-x", uid, _master_admin()))
    assert out["removed"] is True
    assert out["removed_roles"] == ["viewer"]
    viewer_gid = kc._resolve_group_tree("tenant-x")["/tenant-x/viewer"]
    assert uid not in fake.memberships[viewer_gid]


def test_remove_member_route_not_member_404(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    with pytest.raises(HTTPException) as exc:
        _run(api.remove_tenant_member_route("tenant-x", "ghost-uid", _master_admin()))
    assert exc.value.status_code == 404


def test_change_member_role_route(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    fake = kc_configured
    uid = _seed_tenant_member("tenant-x", "carol", "viewer")
    out = _run(api.change_tenant_member_role_route(
        "tenant-x", uid, {"role": "admin"}, _master_admin()
    ))
    assert out["role"] == "admin"
    assert out["previous_roles"] == ["viewer"]
    tree = kc._resolve_group_tree("tenant-x")
    assert uid in fake.memberships[tree["/tenant-x/admin"]]


def test_change_member_role_bad_role_400(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    uid = _seed_tenant_member("tenant-x", "carol", "viewer")
    with pytest.raises(HTTPException) as exc:
        _run(api.change_tenant_member_role_route(
            "tenant-x", uid, {"role": "master_admin"}, _master_admin()
        ))
    assert exc.value.status_code == 400


def test_change_member_role_not_member_404(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    with pytest.raises(HTTPException) as exc:
        _run(api.change_tenant_member_role_route(
            "tenant-x", "ghost-uid", {"role": "admin"}, _master_admin()
        ))
    assert exc.value.status_code == 404


def test_reset_member_password_route(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    fake = kc_configured
    uid = _seed_tenant_member("tenant-x", "erin", "viewer")
    out = _run(api.reset_tenant_member_password_route("tenant-x", uid, _master_admin()))
    assert out["temporary_password"]
    assert fake.passwords[uid]["value"] == out["temporary_password"]


def test_reset_member_password_not_member_404(db_connection, monkeypatch, kc_configured):
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    with pytest.raises(HTTPException) as exc:
        _run(api.reset_tenant_member_password_route("tenant-x", "ghost-uid", _master_admin()))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Member-management routes: per-tenant authorization matrix
# ---------------------------------------------------------------------------


def test_tenant_admin_manages_own_members(db_connection, monkeypatch, kc_configured):
    """A tenant admin (manage_users in that tenant) may list + mutate its members."""
    _patch_marqo(monkeypatch)
    uid = _seed_tenant_member("tenant-x", "bob", "viewer")
    admin = _tenant_admin_in("tenant-x")

    # List (200)
    members = _run(api.list_tenant_members_route("tenant-x", admin))
    assert any(m["username"] == "bob" for m in members)
    # Add a member (200)
    added = _run(api.create_tenant_member_route(
        "tenant-x", {"username": "frank", "role": "content_curator"}, admin
    ))
    assert added["created"] is True
    # Change role (200)
    changed = _run(api.change_tenant_member_role_route(
        "tenant-x", uid, {"role": "admin"}, admin
    ))
    assert changed["role"] == "admin"
    # Reset password (200)
    reset = _run(api.reset_tenant_member_password_route("tenant-x", uid, admin))
    assert reset["temporary_password"]


def test_tenant_admin_cannot_manage_other_tenant_404(db_connection, monkeypatch, kc_configured):
    """Cross-tenant member management is hidden as 404 (never 403)."""
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    uid = _seed_tenant_member("tenant-y", "bob", "viewer")
    admin_x = _tenant_admin_in("tenant-x")

    with pytest.raises(HTTPException) as exc:
        _run(api.list_tenant_members_route("tenant-y", admin_x))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc2:
        _run(api.remove_tenant_member_route("tenant-y", uid, admin_x))
    assert exc2.value.status_code == 404


def test_viewer_cannot_manage_members_403(db_connection, monkeypatch, kc_configured):
    """A member of the tenant with an insufficient role gets 403 (not 404)."""
    _patch_marqo(monkeypatch)
    _seed_tenant_member("tenant-x", "bob", "viewer")
    viewer = _viewer_in("tenant-x")

    with pytest.raises(HTTPException) as exc:
        _run(api.list_tenant_members_route("tenant-x", viewer))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc2:
        _run(api.create_tenant_member_route(
            "tenant-x", {"username": "x", "role": "viewer"}, viewer
        ))
    assert exc2.value.status_code == 403


def test_platform_admin_manages_any_tenant(db_connection, monkeypatch, kc_configured):
    """A master_admin (no tenant membership) may manage any known tenant's members."""
    _patch_marqo(monkeypatch)
    uid = _seed_tenant_member("tenant-x", "bob", "viewer")
    members = _run(api.list_tenant_members_route("tenant-x", _master_admin()))
    assert any(m["username"] == "bob" for m in members)
    out = _run(api.reset_tenant_member_password_route("tenant-x", uid, _master_admin()))
    assert out["temporary_password"]


def test_manage_members_unknown_tenant_404(db_connection, monkeypatch, kc_configured):
    """Even a platform admin gets 404 on an unknown tenant."""
    _patch_marqo(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _run(api.list_tenant_members_route("ghost", _master_admin()))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Security regressions: cross-tenant / privilege-escalation guards
# ---------------------------------------------------------------------------


def _seed_platform_admin_account(fake, username: str = "platform-root") -> str:
    """A realm account holding the platform-admin realm role, in no tenant group."""
    uid = fake._new_id("usr")
    fake.users[uid] = {"id": uid, "username": username, "email": f"{username}@corp.example"}
    fake.passwords[uid] = {"type": "password", "temporary": False, "value": "root-pw"}
    fake.realm_roles[uid] = {"master_admin"}
    return uid


def test_tenant_admin_cannot_absorb_then_reset_foreign_account(
    db_connection, monkeypatch, kc_configured
):
    """BLOCKER: absorb-then-reset realm takeover.

    Before the fix, a tenant admin could POST /tenants/<its own>/members with the
    username of ANY realm account: ``_find_user_by_username`` is realm-wide, the
    merge branch joined that account to the attacker's group tree (granting it the
    attacker's tenant data), and ``_member_role_groups`` then saw it as an ordinary
    member — so reset-password returned the victim's new credential in the body.
    """
    _patch_marqo(monkeypatch)
    fake = kc_configured
    _run(api.create_tenant_route({"instance": "tenant-evil"}, _master_admin()))
    victim = _seed_platform_admin_account(fake)
    attacker = _tenant_admin_in("tenant-evil")

    # Step 1 — absorb the victim into the attacker's tenant: refused.
    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_member_route(
            "tenant-evil", {"username": "platform-root", "role": "viewer"}, attacker
        ))
    assert exc.value.status_code == 403
    viewer_gid = kc._resolve_group_tree("tenant-evil")["/tenant-evil/viewer"]
    assert victim not in fake.memberships.get(viewer_gid, set())

    # Step 2 — with no membership the victim is simply not visible: 404, no reset.
    with pytest.raises(HTTPException) as exc2:
        _run(api.reset_tenant_member_password_route("tenant-evil", victim, attacker))
    assert exc2.value.status_code == 404
    assert fake.passwords[victim]["value"] == "root-pw"


def test_platform_admin_account_is_never_mutable_through_member_routes(
    db_connection, monkeypatch, kc_configured
):
    """Second layer: even WITH a tenant group membership, an account holding a
    platform-admin realm role is off-limits to the member routes."""
    _patch_marqo(monkeypatch)
    fake = kc_configured
    _run(api.create_tenant_route({"instance": "tenant-evil"}, _master_admin()))
    victim = _seed_platform_admin_account(fake)
    # Simulate the membership the absorb step used to create.
    viewer_gid = kc._resolve_group_tree("tenant-evil")["/tenant-evil/viewer"]
    fake.memberships.setdefault(viewer_gid, set()).add(victim)
    attacker = _tenant_admin_in("tenant-evil")

    for call in (
        lambda: api.reset_tenant_member_password_route("tenant-evil", victim, attacker),
        lambda: api.remove_tenant_member_route("tenant-evil", victim, attacker),
        lambda: api.change_tenant_member_role_route(
            "tenant-evil", victim, {"role": "admin"}, attacker
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            _run(call())
        assert exc.value.status_code == 403
    assert fake.passwords[victim]["value"] == "root-pw"
    # A platform admin is equally refused — the account is protected, not scoped.
    with pytest.raises(HTTPException) as exc2:
        _run(api.reset_tenant_member_password_route("tenant-evil", victim, _master_admin()))
    assert exc2.value.status_code == 403


def test_protected_role_held_via_composite_is_still_protected(
    db_connection, monkeypatch, kc_configured
):
    """M1: a protected realm role reached through a COMPOSITE role must protect.

    ``/role-mappings/realm`` returns only DIRECTLY assigned roles, so a victim whose
    ``master_admin`` arrives via a composite (here ``tenant_support`` -> ``master_admin``)
    reads back as an ordinary account and the guard waves the mutation through — the
    realm-takeover path this guard exists to close, reopened through a side door.
    """
    _patch_marqo(monkeypatch)
    fake = kc_configured
    _run(api.create_tenant_route({"instance": "tenant-evil"}, _master_admin()))
    victim = _seed_platform_admin_account(fake, username="composite-root")
    # The ONLY direct mapping is the innocuous-looking wrapper role.
    fake.realm_roles[victim] = {"tenant_support"}
    fake.composite_roles["tenant_support"] = {"master_admin"}
    viewer_gid = kc._resolve_group_tree("tenant-evil")["/tenant-evil/viewer"]
    fake.memberships.setdefault(viewer_gid, set()).add(victim)

    # platform_admin=True isolates the protected-role check from the cross-tenant one.
    with pytest.raises(kc.KeycloakAdminForbidden):
        kc.assert_target_manageable("tenant-evil", victim, platform_admin=True)
    assert kc._user_realm_roles(victim) & kc.PROTECTED_REALM_ROLES, (
        "effective realm roles must expose the composite-derived platform-admin role"
    )

    # ...and the member routes must refuse the tenant admin end to end.
    attacker = _tenant_admin_in("tenant-evil")
    with pytest.raises(HTTPException) as exc:
        _run(api.reset_tenant_member_password_route("tenant-evil", victim, attacker))
    assert exc.value.status_code == 403
    assert fake.passwords[victim]["value"] == "root-pw"


def test_protected_role_held_via_group_membership_is_still_protected(
    db_connection, monkeypatch, kc_configured
):
    """M1: a protected realm role reached through GROUP membership must protect.

    The victim has no direct realm role at all: ``master_admin`` is mapped onto the
    ``/platform-ops`` group it belongs to. Only the effective role set sees it.
    """
    _patch_marqo(monkeypatch)
    fake = kc_configured
    _run(api.create_tenant_route({"instance": "tenant-evil"}, _master_admin()))
    victim = _seed_platform_admin_account(fake, username="group-root")
    fake.realm_roles[victim] = set()  # nothing assigned directly

    ops_gid = kc._ensure_top_group("platform-ops")
    fake.group_realm_roles[ops_gid] = {"master_admin"}
    fake.memberships.setdefault(ops_gid, set()).add(victim)
    viewer_gid = kc._resolve_group_tree("tenant-evil")["/tenant-evil/viewer"]
    fake.memberships.setdefault(viewer_gid, set()).add(victim)

    with pytest.raises(kc.KeycloakAdminForbidden):
        kc.assert_target_manageable("tenant-evil", victim, platform_admin=True)
    assert kc._user_realm_roles(victim) & kc.PROTECTED_REALM_ROLES, (
        "effective realm roles must expose the group-derived platform-admin role"
    )

    attacker = _tenant_admin_in("tenant-evil")
    with pytest.raises(HTTPException) as exc:
        _run(api.remove_tenant_member_route("tenant-evil", victim, attacker))
    assert exc.value.status_code == 403
    assert victim in fake.memberships[viewer_gid]


def test_ordinary_member_without_protected_roles_stays_manageable(
    db_connection, monkeypatch, kc_configured
):
    """Counterpart to the two tests above: widening to the effective role set must
    not turn the guard into a blanket refusal. A member whose composite/group roles
    contain nothing protected is still manageable."""
    _patch_marqo(monkeypatch)
    fake = kc_configured
    _run(api.create_tenant_route({"instance": "tenant-ok"}, _master_admin()))
    _run(api.create_tenant_member_route(
        "tenant-ok", {"username": "plain-user", "role": "viewer"}, _master_admin()
    ))
    uid = next(u["id"] for u in fake.users.values() if u["username"] == "plain-user")
    fake.realm_roles[uid] = {"reporting"}
    fake.composite_roles["reporting"] = {"reporting_read"}

    kc.assert_target_manageable("tenant-ok", uid)  # must not raise
    result = _run(api.reset_tenant_member_password_route(
        "tenant-ok", uid, _tenant_admin_in("tenant-ok")
    ))
    assert result.get("temporary_password")


def test_tenant_admin_cannot_mutate_member_of_another_tenant(
    db_connection, monkeypatch, kc_configured
):
    """Third layer: a target that also belongs to another tenant is refused for a
    tenant admin (a platform admin may still manage it)."""
    _patch_marqo(monkeypatch)
    fake = kc_configured
    uid = _seed_tenant_member("tenant-good", "shared", "admin")
    _run(api.create_tenant_route({"instance": "tenant-evil"}, _master_admin()))
    viewer_gid = kc._resolve_group_tree("tenant-evil")["/tenant-evil/viewer"]
    fake.memberships.setdefault(viewer_gid, set()).add(uid)

    with pytest.raises(HTTPException) as exc:
        _run(api.reset_tenant_member_password_route(
            "tenant-evil", uid, _tenant_admin_in("tenant-evil")
        ))
    assert exc.value.status_code == 403
    # The platform admin is exempt from the cross-tenant check.
    out = _run(api.reset_tenant_member_password_route("tenant-evil", uid, _master_admin()))
    assert out["temporary_password"]


def test_tenant_admin_cannot_add_existing_realm_username(
    db_connection, monkeypatch, kc_configured
):
    """The merge branch is platform-admin-only: a tenant admin gets 403 on a
    username that already exists anywhere in the realm."""
    _patch_marqo(monkeypatch)
    fake = kc_configured
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    uid = fake._new_id("usr")
    fake.users[uid] = {"id": uid, "username": "outsider"}

    with pytest.raises(HTTPException) as exc:
        _run(api.create_tenant_member_route(
            "tenant-x", {"username": "outsider", "role": "viewer"}, _tenant_admin_in("tenant-x")
        ))
    assert exc.value.status_code == 403
    viewer_gid = kc._resolve_group_tree("tenant-x")["/tenant-x/viewer"]
    assert uid not in fake.memberships.get(viewer_gid, set())
    # The platform admin can still merge deliberately.
    out = _run(api.create_tenant_member_route(
        "tenant-x", {"username": "outsider", "role": "viewer"}, _master_admin()
    ))
    assert out["created"] is False


# ---------------------------------------------------------------------------
# Security regressions: self-mutation, last-admin, ordering, error accumulation
# ---------------------------------------------------------------------------


def test_admin_cannot_remove_or_demote_itself(db_connection, monkeypatch, kc_configured):
    """A tenant admin acting on its OWN user_id is refused (403) — otherwise it can
    demote/remove itself out of the tenant it administers."""
    _patch_marqo(monkeypatch)
    uid = _seed_tenant_member("tenant-x", "selfadmin", "admin")
    _run(api.create_tenant_member_route(
        "tenant-x", {"username": "backup", "role": "admin"}, _master_admin()
    ))
    myself = claims_to_user({"sub": uid, "tenant_roles": {"tenant-x": ["admin"]}})

    with pytest.raises(HTTPException) as exc:
        _run(api.remove_tenant_member_route("tenant-x", uid, myself))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc2:
        _run(api.change_tenant_member_role_route("tenant-x", uid, {"role": "viewer"}, myself))
    assert exc2.value.status_code == 403


def test_last_admin_cannot_be_removed_or_demoted(db_connection, monkeypatch, kc_configured):
    """Removing/demoting a tenant's ONLY admin would leave nobody with manage_users
    in it — refused with 409 for a tenant admin caller."""
    _patch_marqo(monkeypatch)
    uid = _seed_tenant_member("tenant-x", "solo", "admin")
    _run(api.create_tenant_member_route(
        "tenant-x", {"username": "bystander", "role": "viewer"}, _master_admin()
    ))
    caller = _tenant_admin_in("tenant-x")

    with pytest.raises(HTTPException) as exc:
        _run(api.remove_tenant_member_route("tenant-x", uid, caller))
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc2:
        _run(api.change_tenant_member_role_route("tenant-x", uid, {"role": "viewer"}, caller))
    assert exc2.value.status_code == 409
    # Promoting a second admin first makes the demotion legal again.
    other = _run(api.list_tenant_members_route("tenant-x", caller))
    bystander = next(m for m in other if m["username"] == "bystander")
    _run(api.change_tenant_member_role_route(
        "tenant-x", bystander["user_id"], {"role": "admin"}, caller
    ))
    out = _run(api.change_tenant_member_role_route("tenant-x", uid, {"role": "viewer"}, caller))
    assert out["role"] == "viewer"


def test_set_member_role_join_failure_keeps_previous_role(
    db_connection, monkeypatch, kc_configured
):
    """The target group is joined BEFORE the old ones are dropped: a failing join
    must leave the member's existing role intact, never zero roles."""
    _patch_marqo(monkeypatch)
    fake = kc_configured
    uid = _seed_tenant_member("tenant-x", "carol", "viewer")
    tree = kc._resolve_group_tree("tenant-x")
    fake.fail_join_groups.add(tree["/tenant-x/admin"])

    with pytest.raises(kc.KeycloakAdminError):
        kc.set_member_role("tenant-x", uid, "admin")
    # Still a viewer — not silently ejected from the tenant.
    assert uid in fake.memberships[tree["/tenant-x/viewer"]]
    assert kc.list_members("tenant-x")[0]["roles"] == ["viewer"]


def test_remove_from_group_accumulates_per_group_failures(
    db_connection, monkeypatch, kc_configured
):
    """A failing detach must not abort the loop: the remaining groups are still
    attempted and the caller is told which ones failed (half-removed state)."""
    _patch_marqo(monkeypatch)
    fake = kc_configured
    uid = _seed_tenant_member("tenant-x", "multi", "viewer")
    _run(api.create_tenant_member_route(
        "tenant-x", {"username": "multi", "role": "admin"}, _master_admin()
    ))
    tree = kc._resolve_group_tree("tenant-x")
    fake.fail_delete_groups.add(tree["/tenant-x/viewer"])

    out = _run(api.remove_tenant_member_route("tenant-x", uid, _master_admin()))
    assert out["removed"] is False
    assert out["removed_roles"] == ["admin"]
    assert out["failed_roles"] == ["viewer"]
    # The admin detach still happened even though viewer failed first alphabetically.
    assert uid not in fake.memberships[tree["/tenant-x/admin"]]
    # And the reported failure carries no Keycloak internals.
    assert all(isinstance(role, str) and "http" not in role for role in out["failed_roles"])


def test_member_route_errors_do_not_leak_keycloak_internals(
    db_connection, monkeypatch, kc_configured
):
    """A raw ``_admin_call`` error embeds the in-cluster admin URL / realm; tenant
    admins must get a generic message instead."""
    _patch_marqo(monkeypatch)
    _run(api.create_tenant_route({"instance": "tenant-x"}, _master_admin()))
    leaky = "GET http://keycloak:8080/auth/admin/realms/docs-pipeline/groups -> 500: boom"

    def _boom(*_args, **_kwargs):
        raise kc.KeycloakAdminError(leaky)

    monkeypatch.setattr(kc, "list_members", _boom)
    with pytest.raises(HTTPException) as exc:
        _run(api.list_tenant_members_route("tenant-x", _tenant_admin_in("tenant-x")))
    assert exc.value.status_code == 502
    assert "keycloak:8080" not in exc.value.detail
    assert "docs-pipeline" not in exc.value.detail
    assert "boom" not in exc.value.detail


# ---------------------------------------------------------------------------
# UI regressions (source-level: the console has no JS test runner)
# ---------------------------------------------------------------------------


def _ui_source(relative: str) -> str:
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "ui" / "src" / relative
    if not path.exists():
        pytest.skip(f"UI source not present: {relative}")
    return path.read_text(encoding="utf-8")


def test_sidebar_tenants_entry_is_reachable_by_tenant_admins():
    """The Tenants link must not be master_admin-only, or a tenant admin can never
    reach the member management this surface exists for."""
    source = _ui_source("components/AppSidebar.jsx")
    entry = next(line for line in source.splitlines() if "'/tenants'" in line)
    # Not a single master_admin-only gate: master_admin OR manage_users.
    assert "anyOf" in entry
    assert "manage_users" in entry
    assert "anyOf" in source.replace(entry, "")  # the filter honours `anyOf`


def test_tenants_view_confirms_destructive_member_actions():
    """Remove-member and reset-password are unrecoverable single clicks without a
    confirmation dialog."""
    source = _ui_source("views/TenantsView.jsx")
    assert "AlertDialog" in source
    assert "setConfirmAction" in source
