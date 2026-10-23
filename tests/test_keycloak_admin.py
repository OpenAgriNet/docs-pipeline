"""Keycloak Admin client and the tenant user/member provisioning routes.

``keycloak_admin._http_request`` is replaced by ``FakeKeycloak``, a small stateful
Keycloak Admin REST server; tenant creation provisions into ``fake_marqo``.
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse

import pytest
from fastapi import HTTPException

import pipeline.db as db_mod
import pipeline.keycloak_admin as kc
from pipeline.routers import tenants as tenant_routes
from tests.helpers import http_status, master_admin, member

GROUPS_X = {"/tenant-x", "/tenant-x/admin", "/tenant-x/content_curator", "/tenant-x/viewer"}


class FakeKeycloak:
    """In-memory Keycloak Admin REST, called in place of ``keycloak_admin._http_request``.

    Realm roles are computed the way Keycloak does: direct mappings, plus roles
    mapped onto any group the user is in (inherited down the group tree), closed
    over composites. Only ``.../role-mappings/realm/composite`` reports that
    effective set; ``.../role-mappings/realm`` lists the direct mappings only.
    A membership PUT / DELETE on a group in ``fail_join_groups`` /
    ``fail_delete_groups`` raises.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.orgs: list[dict] = []
        self.groups: dict[str, dict] = {}  # id -> {id, name, parent}
        self.users: dict[str, dict] = {}  # id -> representation
        self.memberships: dict[str, set[str]] = {}  # group id -> user ids
        self.org_members: dict[str, set[str]] = {}  # org id -> user ids
        self.passwords: dict[str, dict] = {}  # user id -> credential
        self.realm_roles: dict[str, set[str]] = {}  # user id -> DIRECT realm roles
        self.composite_roles: dict[str, set[str]] = {}  # role -> the realm roles it implies
        self.group_realm_roles: dict[str, set[str]] = {}  # group id -> realm roles mapped onto it
        self.fail_join_groups: set[str] = set()
        self.fail_delete_groups: set[str] = set()
        self._seq = 0

    def _new_id(self, prefix):
        self._seq += 1
        return f"{prefix}-{self._seq}"

    def add_user(self, username, password=None, **rep):
        """Seed a realm account directly (no route); returns its id."""
        uid = self._new_id("usr")
        self.users[uid] = {"id": uid, "username": username, **rep}
        if password:
            self.passwords[uid] = {"type": "password", "temporary": False, "value": password}
        return uid

    def join(self, gid, uid):
        self.memberships.setdefault(gid, set()).add(uid)
        return gid

    def called(self, method, pattern) -> bool:
        return any(m == method and re.search(pattern, u) for m, u, _ in self.calls)

    def _add_group(self, name, parent):
        gid = self._new_id("grp")
        self.groups[gid] = {"id": gid, "name": name, "parent": parent}
        return 201, None

    def _lineage(self, gid):
        """The group, then each ancestor up to the top."""
        group = self.groups.get(gid)
        while group:
            yield group
            group = self.groups.get(group["parent"])

    def _path(self, gid):
        return "/" + "/".join(g["name"] for g in reversed([*self._lineage(gid)]))

    def _effective_realm_roles(self, uid) -> set[str]:
        pending = set(self.realm_roles.get(uid, ()))
        for gid, members in self.memberships.items():
            if uid in members:
                for group in self._lineage(gid):
                    pending |= self.group_realm_roles.get(group["id"], set())
        effective: set[str] = set()
        while pending:
            role = pending.pop()
            if role not in effective:
                effective.add(role)
                pending |= self.composite_roles.get(role, set())
        return effective

    def __call__(self, method, url, *, token=None, body=None, form=None, timeout=30):
        self.calls.append((method, url, body if body is not None else form))
        if "openid-connect/token" in url:
            return 200, {"access_token": "fake-admin-token", "expires_in": 300}
        base = kc._admin_base_url()
        assert url.startswith(base), f"unexpected url {url}"
        path, _, query = url[len(base):].partition("?")
        query = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        match method, path.strip("/").split("/"):
            case "GET", ["organizations"]:
                return 200, list(self.orgs)
            case "POST", ["organizations"]:
                self.orgs.append({"id": self._new_id("org"), "name": body["name"], "alias": body.get("alias")})
                return 201, None
            case "POST", ["organizations", org, "members"]:
                self.org_members.setdefault(org, set()).add(body)
                return 204, None
            case "GET", ["groups"]:
                search = query.get("search")
                tops = [g for g in self.groups.values() if g["parent"] is None]
                return 200, [g for g in tops if search is None or search in g["name"]]
            case "POST", ["groups"]:
                return self._add_group(body["name"], None)
            case "GET", ["groups", gid, "children"]:
                return 200, [g for g in self.groups.values() if g["parent"] == gid]
            case "POST", ["groups", gid, "children"]:
                return self._add_group(body["name"], gid)
            case "GET", ["groups", gid, "members"]:
                return 200, [self.users[uid] for uid in self.memberships.get(gid, set())]
            case "GET", ["users"]:
                return 200, [u for u in self.users.values() if u["username"] == query.get("username")]
            case "POST", ["users"]:
                uid = self._new_id("usr")
                self.users[uid] = {"id": uid, **body}
                return 201, None
            case "PUT", ["users", uid]:
                self.users[uid] = {"id": uid, **body}
                return 204, None
            case "PUT", ["users", uid, "reset-password"]:
                self.passwords[uid] = body
                return 204, None
            case "GET", ["users", uid, "role-mappings", "realm", "composite"]:
                return 200, [{"name": r} for r in sorted(self._effective_realm_roles(uid))]
            case "GET", ["users", uid, "role-mappings", "realm"]:
                return 200, [{"name": r} for r in sorted(self.realm_roles.get(uid, set()))]
            case "GET", ["users", uid, "groups"]:
                joined = [gid for gid, ids in self.memberships.items() if uid in ids]
                return 200, [{"id": gid, "path": self._path(gid)} for gid in joined]
            case "PUT", ["users", uid, "groups", gid]:
                if gid in self.fail_join_groups:
                    raise kc.KeycloakAdminError(f"PUT {path} -> 500: join refused")
                self.join(gid, uid)
                return 204, None
            case "DELETE", ["users", uid, "groups", gid]:
                if gid in self.fail_delete_groups:
                    raise kc.KeycloakAdminError(f"DELETE {path} -> 500: detach refused")
                self.memberships.setdefault(gid, set()).discard(uid)
                return 204, None
        raise AssertionError(f"unhandled fake KC route: {method} {path}")


@pytest.fixture
def fake_kc(monkeypatch, db_connection, fake_marqo):
    """KC admin configured against a ``FakeKeycloak``; yields the fake."""
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


@pytest.fixture(params=[None, kc.PLACEHOLDER_ADMIN_SECRET], ids=["unset", "placeholder"])
def unconfigured(request, monkeypatch):
    """No usable admin secret. The well-known placeholder must count as none (C1)."""
    if request.param is None:
        monkeypatch.delenv("KEYCLOAK_ADMIN_CLIENT_SECRET", raising=False)
    else:
        monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", request.param)
    kc.reset_token_cache()


def _tenant(instance):
    return asyncio.run(tenant_routes.create_tenant_route({"instance": instance}, master_admin()))


def _member_of(instance, username, role="viewer", caller=None):
    """POST ``username`` as ``role`` (by the platform admin unless ``caller``); returns the response."""
    payload = {"username": username, "role": role}
    return asyncio.run(tenant_routes.create_tenant_member_route(instance, payload, caller or master_admin()))


def _seed(instance, username, role="viewer"):
    """Create the tenant (adopted if it exists) and one member; returns the member's user id."""
    _tenant(instance)
    return _member_of(instance, username, role)["user_id"]


def _group(instance, role):
    return kc._resolve_group_tree(instance)[f"/{instance}/{role}"]


def _platform_root(fake_kc):
    """A realm account holding ``master_admin`` directly, in no tenant group."""
    uid = fake_kc.add_user("platform-root", "root-pw", email="platform-root@corp.example")
    fake_kc.realm_roles[uid] = {"master_admin"}
    return uid


# --- keycloak_admin module -------------------------------------------------------


def test_token_endpoints_admin_host_first_then_issuer(monkeypatch):
    """The token must be minted on the Admin API's host so ``iss`` matches (else KC 401s);
    the public issuer is only a fallback."""
    monkeypatch.setenv("KEYCLOAK_ADMIN_BASE_URL", "http://keycloak:8080/auth")
    monkeypatch.setenv("KEYCLOAK_ISSUER", "https://sso.example.com/auth/realms/docs-pipeline")
    monkeypatch.setenv(
        "KEYCLOAK_JWKS_URL", "http://keycloak:8080/auth/realms/docs-pipeline/protocol/openid-connect/certs"
    )
    endpoints = kc._token_endpoints()
    assert endpoints[0] == "http://keycloak:8080/auth/realms/docs-pipeline/protocol/openid-connect/token"
    assert "https://sso.example.com/auth/realms/docs-pipeline/protocol/openid-connect/token" in endpoints
    assert len(endpoints) == len(set(endpoints)), "the JWKS-derived endpoint duplicates the admin host's"


def test_admin_base_url_defaults(monkeypatch):
    monkeypatch.delenv("KEYCLOAK_ADMIN_BASE_URL", raising=False)
    monkeypatch.delenv("KEYCLOAK_REALM", raising=False)
    assert kc._admin_base_url() == "http://keycloak:8080/auth/admin/realms/docs-pipeline"


def test_unusable_secret_is_unconfigured(unconfigured):
    assert kc.is_configured() is False
    with pytest.raises(kc.KeycloakAdminUnconfigured):
        kc._admin_token()
    with pytest.raises(kc.KeycloakAdminUnconfigured):
        kc.list_members("tenant-x")


def test_find_user_by_username_requires_exact_match(monkeypatch):
    """L5: never a fuzzy KC hit, only an exact (case-insensitive) one."""
    monkeypatch.setattr(kc, "_admin_call", lambda *a, **k: (200, [{"id": "x", "username": "alice-2"}]))
    assert kc._find_user_by_username("alice") is None
    monkeypatch.setattr(kc, "_admin_call", lambda *a, **k: (200, [{"id": "y", "username": "Alice"}]))
    assert kc._find_user_by_username("alice")["id"] == "y"


def test_create_user_existing_is_merge_only(fake_kc):
    """H3: an existing username only gains the group (platform-admin ``allow_existing``);
    its representation and password stay, and no password is returned."""
    kc.ensure_group_tree("tenant-x")
    uid = fake_kc.add_user(
        "alice", "original-pw", email="real@corp.example",
        attributes={"instances": ["tenant-x"], "custom": ["keep-me"]},
    )
    out = kc.create_user("alice", None, "New-Temp-Pass-1!", "/tenant-x/viewer", allow_existing=True)
    assert out["created"] is False
    assert "temporary_password" not in out
    assert out["id"] == uid
    assert fake_kc.users[uid]["email"] == "real@corp.example"
    assert fake_kc.users[uid]["attributes"]["custom"] == ["keep-me"]
    assert fake_kc.passwords[uid]["value"] == "original-pw"
    assert uid in fake_kc.memberships[_group("tenant-x", "viewer")]


def test_ensure_group_tree_creates_children(fake_kc):
    assert set(kc.ensure_group_tree("tenant-x")) == GROUPS_X
    def top_group_posts():
        return [c for c in fake_kc.calls if c[0] == "POST" and c[1].endswith("/groups")]

    before = top_group_posts()
    kc.ensure_group_tree("tenant-x")
    assert top_group_posts() == before, "the top group was re-created"


def test_create_user_sets_password_and_membership(fake_kc):
    kc.ensure_group_tree("tenant-x")
    uid = kc.create_user(
        username="alice", email=None, temporary_password="Temp-Pass-123!", group_path="/tenant-x/admin"
    )["id"]
    assert fake_kc.users[uid]["firstName"], "KC26 requires firstName and lastName"
    assert fake_kc.users[uid]["lastName"]
    assert fake_kc.users[uid]["emailVerified"] is True
    assert fake_kc.passwords[uid]["temporary"] is True
    assert fake_kc.passwords[uid]["value"] == "Temp-Pass-123!"
    assert uid in fake_kc.memberships[_group("tenant-x", "admin")]


def test_list_members_reports_roles(fake_kc):
    kc.ensure_group_tree("tenant-x")
    kc.create_user("alice", None, "Temp-Pass-123!", "/tenant-x/admin")
    kc.create_user("bob", "bob@x.example.com", "Temp-Pass-456!", "/tenant-x/viewer")
    by_name = {m["username"]: m for m in kc.list_members("tenant-x")}
    assert by_name["alice"]["roles"] == ["admin"]
    assert by_name["bob"]["roles"] == ["viewer"]
    assert by_name["bob"]["email"] == "bob@x.example.com"


def test_generate_temporary_password_is_strong():
    pwd = kc.generate_temporary_password()
    assert len(pwd) >= 16
    for kind in (str.islower, str.isupper, str.isdigit):
        assert any(kind(c) for c in pwd), kind.__name__


def test_remove_from_group_detaches_all_roles(fake_kc):
    uid = _seed("tenant-x", "bob")
    assert uid in fake_kc.memberships[_group("tenant-x", "viewer")]
    out = kc.remove_from_group("tenant-x", uid)
    assert out["was_member"] is True
    assert out["removed_roles"] == ["viewer"]
    assert uid not in fake_kc.memberships[_group("tenant-x", "viewer")]


def test_set_member_role_swaps_to_single_role(fake_kc):
    uid = _seed("tenant-x", "carol")
    out = kc.set_member_role("tenant-x", uid, "admin")
    assert out["was_member"] is True
    assert out["role"] == "admin"
    assert out["previous_roles"] == ["viewer"]
    assert uid in fake_kc.memberships[_group("tenant-x", "admin")]
    assert uid not in fake_kc.memberships[_group("tenant-x", "viewer")]


def test_set_member_role_rejects_platform_role(fake_kc):
    uid = _seed("tenant-x", "dave")
    with pytest.raises(kc.KeycloakAdminError):
        kc.set_member_role("tenant-x", uid, "master_admin")


def test_reset_password_sets_temporary_credential(fake_kc):
    uid = _seed("tenant-x", "erin")
    out = kc.reset_password("tenant-x", uid)
    assert out["was_member"] is True
    assert out["temporary_password"]
    assert fake_kc.passwords[uid]["value"] == out["temporary_password"]
    assert fake_kc.passwords[uid]["temporary"] is True


@pytest.mark.parametrize(
    "primitive, expected",
    [
        (lambda: kc.remove_from_group("tenant-x", "ghost-uid"), {"was_member": False, "removed_roles": []}),
        (lambda: kc.set_member_role("tenant-x", "ghost-uid", "admin"), {"was_member": False}),
        (lambda: kc.reset_password("tenant-x", "ghost-uid"), {"was_member": False, "temporary_password": None}),
    ],
    ids=["remove", "set-role", "reset-password"],
)
def test_primitive_reports_a_non_member(fake_kc, primitive, expected):
    _tenant("tenant-x")
    out = primitive()
    assert {key: out[key] for key in expected} == expected


# --- routes ----------------------------------------------------------------------


def test_create_tenant_calls_org_and_group_tree(fake_kc):
    out = asyncio.run(
        tenant_routes.create_tenant_route({"instance": "tenant-x", "display_name": "Tenant X"}, master_admin())
    )
    assert out["tenant"]["id"] == "tenant-x"
    assert fake_kc.called("POST", r"/organizations$")
    assert set(out["keycloak"]["groups"]) == GROUPS_X
    assert "warning" not in out


def test_create_tenant_graceful_when_unconfigured(db_connection, fake_marqo, monkeypatch):
    """The app-side tenant is still created; the identity plane is skipped with a warning."""
    monkeypatch.delenv("KEYCLOAK_ADMIN_CLIENT_SECRET", raising=False)
    kc.reset_token_cache()
    out = _tenant("tenant-y")
    assert out["tenant"]["id"] == "tenant-y"
    assert out["keycloak"] is None
    assert "warning" in out
    assert db_mod.get_tenant("tenant-y") is not None


def test_member_routes_503_when_unconfigured(unconfigured, db_connection):
    db_mod.create_tenant("tenant-z")  # app side only, so the routes reach the Keycloak call
    create_admin = tenant_routes.create_tenant_admin_route("tenant-z", {"username": "alice"}, master_admin())
    assert http_status(create_admin) == 503
    assert http_status(tenant_routes.list_tenant_members_route("tenant-z", master_admin())) == 503


def test_create_admin_returns_temp_password(fake_kc):
    _tenant("tenant-x")
    out = asyncio.run(tenant_routes.create_tenant_admin_route("tenant-x", {"username": "alice"}, master_admin()))
    assert out["username"] == "alice"
    assert out["temporary_password"]
    assert fake_kc.called("POST", r"/users$")
    assert fake_kc.called("PUT", r"/users/[^/]+/reset-password$")
    assert fake_kc.called("PUT", r"/users/[^/]+/groups/[^/]+$")
    assert fake_kc.memberships.get(_group("tenant-x", "admin"))


def test_create_admin_existing_user_merges_without_password(fake_kc):
    """H3 (route): re-adding a username merges it into the new group; its password and
    attributes are untouched and no password is returned."""
    _tenant("tenant-x")
    first = asyncio.run(tenant_routes.create_tenant_admin_route("tenant-x", {"username": "alice"}, master_admin()))
    assert first["created"] is True
    assert first["temporary_password"]
    uid = first["user_id"]
    pw_before, attrs_before = dict(fake_kc.passwords[uid]), dict(fake_kc.users[uid].get("attributes") or {})

    second = _member_of("tenant-x", "alice", "viewer")
    assert second["created"] is False
    assert "temporary_password" not in second
    assert second["added_to_group"] == "/tenant-x/viewer"
    assert fake_kc.passwords[uid] == pw_before
    assert (fake_kc.users[uid].get("attributes") or {}) == attrs_before
    assert uid in fake_kc.memberships[_group("tenant-x", "viewer")]


def test_list_members_route(fake_kc):
    _tenant("tenant-x")
    created = asyncio.run(tenant_routes.create_tenant_admin_route("tenant-x", {"username": "alice"}, master_admin()))
    members = asyncio.run(tenant_routes.list_tenant_members_route("tenant-x", master_admin()))
    alice = next(m for m in members if m["username"] == "alice")
    assert "admin" in alice["roles"]
    assert alice["user_id"] == created["user_id"]


def test_remove_member_route(fake_kc):
    uid = _seed("tenant-x", "bob")
    out = asyncio.run(tenant_routes.remove_tenant_member_route("tenant-x", uid, master_admin()))
    assert out["removed"] is True
    assert out["removed_roles"] == ["viewer"]
    assert uid not in fake_kc.memberships[_group("tenant-x", "viewer")]


def test_change_member_role_route(fake_kc):
    uid = _seed("tenant-x", "carol")
    out = asyncio.run(tenant_routes.change_tenant_member_role_route("tenant-x", uid, {"role": "admin"}, master_admin()))
    assert out["role"] == "admin"
    assert out["previous_roles"] == ["viewer"]
    assert uid in fake_kc.memberships[_group("tenant-x", "admin")]


def test_reset_member_password_route(fake_kc):
    uid = _seed("tenant-x", "erin")
    out = asyncio.run(tenant_routes.reset_tenant_member_password_route("tenant-x", uid, master_admin()))
    assert out["temporary_password"]
    assert fake_kc.passwords[uid]["value"] == out["temporary_password"]


REFUSED = {  # (status, call given an existing member's id), all by the platform admin
    "create-admin-unknown-tenant": (
        404, lambda _uid: tenant_routes.create_tenant_admin_route("ghost", {"username": "alice"}, master_admin())
    ),
    "list-unknown-tenant": (404, lambda _uid: tenant_routes.list_tenant_members_route("ghost", master_admin())),
    "remove-non-member": (
        404, lambda _uid: tenant_routes.remove_tenant_member_route("tenant-x", "ghost-uid", master_admin())
    ),
    "change-role-non-member": (404, lambda _uid: tenant_routes.change_tenant_member_role_route(
        "tenant-x", "ghost-uid", {"role": "admin"}, master_admin()
    )),
    "reset-non-member": (
        404, lambda _uid: tenant_routes.reset_tenant_member_password_route("tenant-x", "ghost-uid", master_admin())
    ),
    "create-unknown-role": (400, lambda _uid: tenant_routes.create_tenant_member_route(
        "tenant-x", {"username": "x", "role": "superuser"}, master_admin()
    )),
    "change-to-platform-role": (400, lambda uid: tenant_routes.change_tenant_member_role_route(
        "tenant-x", uid, {"role": "master_admin"}, master_admin()
    )),
}


@pytest.mark.parametrize("status, call", REFUSED.values(), ids=REFUSED.keys())
def test_member_route_refuses_unknown_targets_and_roles(fake_kc, status, call):
    """An unknown tenant or a non-member is 404 even to the platform admin; an unknown or platform role is 400."""
    assert http_status(call(_seed("tenant-x", "carol"))) == status


# --- per-tenant authorization matrix ---------------------------------------------


def test_tenant_admin_manages_own_members(fake_kc):
    uid = _seed("tenant-x", "bob")
    admin = member("tenant-x", "admin")
    members = asyncio.run(tenant_routes.list_tenant_members_route("tenant-x", admin))
    assert any(m["username"] == "bob" for m in members)
    assert _member_of("tenant-x", "frank", "content_curator", admin)["created"] is True
    changed = asyncio.run(tenant_routes.change_tenant_member_role_route("tenant-x", uid, {"role": "admin"}, admin))
    assert changed["role"] == "admin"
    assert asyncio.run(tenant_routes.reset_tenant_member_password_route("tenant-x", uid, admin))["temporary_password"]


def test_tenant_admin_cannot_manage_other_tenant_404(fake_kc):
    """Cross-tenant member management is hidden as 404, never 403."""
    _tenant("tenant-x")
    uid = _seed("tenant-y", "bob")
    admin_x = member("tenant-x", "admin")
    assert http_status(tenant_routes.list_tenant_members_route("tenant-y", admin_x)) == 404
    assert http_status(tenant_routes.remove_tenant_member_route("tenant-y", uid, admin_x)) == 404


def test_viewer_cannot_manage_members_403(fake_kc):
    _seed("tenant-x", "bob")
    viewer = member("tenant-x", "viewer")
    assert http_status(tenant_routes.list_tenant_members_route("tenant-x", viewer)) == 403
    assert http_status(
        tenant_routes.create_tenant_member_route("tenant-x", {"username": "x", "role": "viewer"}, viewer)
    ) == 403


def test_platform_admin_manages_any_tenant(fake_kc):
    uid = _seed("tenant-x", "bob")
    members = asyncio.run(tenant_routes.list_tenant_members_route("tenant-x", master_admin()))
    assert any(m["username"] == "bob" for m in members)
    assert asyncio.run(tenant_routes.reset_tenant_member_password_route("tenant-x", uid, master_admin()))[
        "temporary_password"
    ]


# --- realm takeover and privilege escalation -------------------------------------


def test_tenant_admin_cannot_absorb_then_reset_foreign_account(fake_kc):
    """BLOCKER: ``_find_user_by_username`` is realm-wide, so a tenant admin POSTing any
    realm username to its own tenant used to join that account to its groups, and
    reset-password then returned the victim's new credential."""
    _tenant("tenant-evil")
    victim = _platform_root(fake_kc)
    attacker = member("tenant-evil", "admin")
    absorb = tenant_routes.create_tenant_member_route(
        "tenant-evil", {"username": "platform-root", "role": "viewer"}, attacker
    )
    assert http_status(absorb) == 403
    assert victim not in fake_kc.memberships.get(_group("tenant-evil", "viewer"), set())
    assert http_status(tenant_routes.reset_tenant_member_password_route("tenant-evil", victim, attacker)) == 404
    assert fake_kc.passwords[victim]["value"] == "root-pw"


@pytest.mark.parametrize("held_via", ["direct", "composite", "group"])
def test_platform_admin_account_is_never_mutable_through_member_routes(fake_kc, held_via):
    """Even inside a tenant group, an account that EFFECTIVELY holds ``master_admin`` is
    off-limits to everyone. ``/role-mappings/realm`` lists only direct roles, so one held
    through a composite or a group would read as an ordinary account (M1)."""
    _tenant("tenant-evil")
    victim = _platform_root(fake_kc)
    if held_via == "composite":
        fake_kc.realm_roles[victim] = {"tenant_support"}
        fake_kc.composite_roles["tenant_support"] = {"master_admin"}
    elif held_via == "group":
        fake_kc.realm_roles[victim] = set()
        ops = fake_kc.join(kc._ensure_top_group("platform-ops"), victim)
        fake_kc.group_realm_roles[ops] = {"master_admin"}
    viewer = fake_kc.join(_group("tenant-evil", "viewer"), victim)  # the membership absorb used to create

    with pytest.raises(kc.KeycloakAdminForbidden):  # platform_admin=True: the cross-tenant check is skipped
        kc.assert_target_manageable("tenant-evil", victim, platform_admin=True)
    assert kc._user_realm_roles(victim) & kc.PROTECTED_REALM_ROLES
    attacker = member("tenant-evil", "admin")
    for call in (
        tenant_routes.reset_tenant_member_password_route("tenant-evil", victim, attacker),
        tenant_routes.remove_tenant_member_route("tenant-evil", victim, attacker),
        tenant_routes.change_tenant_member_role_route("tenant-evil", victim, {"role": "admin"}, attacker),
        tenant_routes.reset_tenant_member_password_route("tenant-evil", victim, master_admin()),
    ):
        assert http_status(call) == 403
    assert fake_kc.passwords[victim]["value"] == "root-pw"
    assert victim in fake_kc.memberships[viewer]


def test_ordinary_member_without_protected_roles_stays_manageable(fake_kc):
    """Reading the effective role set must not turn the guard into a blanket refusal."""
    uid = _seed("tenant-ok", "plain-user")
    fake_kc.realm_roles[uid] = {"reporting"}
    fake_kc.composite_roles["reporting"] = {"reporting_read"}
    kc.assert_target_manageable("tenant-ok", uid)  # must not raise
    reset = tenant_routes.reset_tenant_member_password_route("tenant-ok", uid, member("tenant-ok", "admin"))
    assert asyncio.run(reset).get("temporary_password")


def test_tenant_admin_cannot_mutate_member_of_another_tenant(fake_kc):
    """A target that also belongs to another tenant is refused to a tenant admin, not to the platform admin."""
    uid = _seed("tenant-good", "shared", "admin")
    _tenant("tenant-evil")
    fake_kc.join(_group("tenant-evil", "viewer"), uid)
    attacker = member("tenant-evil", "admin")
    assert http_status(tenant_routes.reset_tenant_member_password_route("tenant-evil", uid, attacker)) == 403
    out = asyncio.run(tenant_routes.reset_tenant_member_password_route("tenant-evil", uid, master_admin()))
    assert out["temporary_password"]


def test_tenant_admin_cannot_add_existing_realm_username(fake_kc):
    """The merge branch is platform-admin only."""
    _tenant("tenant-x")
    uid = fake_kc.add_user("outsider")
    add = tenant_routes.create_tenant_member_route(
        "tenant-x", {"username": "outsider", "role": "viewer"}, member("tenant-x", "admin")
    )
    assert http_status(add) == 403
    assert uid not in fake_kc.memberships.get(_group("tenant-x", "viewer"), set())
    assert _member_of("tenant-x", "outsider")["created"] is False


# --- self, last admin, ordering, error accumulation ------------------------------


def test_admin_cannot_remove_or_demote_itself(fake_kc):
    uid = _seed("tenant-x", "selfadmin", "admin")
    _member_of("tenant-x", "backup", "admin")
    myself = member("tenant-x", "admin", sub=uid)
    assert http_status(tenant_routes.remove_tenant_member_route("tenant-x", uid, myself)) == 403
    demote = tenant_routes.change_tenant_member_role_route("tenant-x", uid, {"role": "viewer"}, myself)
    assert http_status(demote) == 403


def test_last_admin_cannot_be_removed_or_demoted(fake_kc):
    """409 for a tenant admin caller; once a second admin exists the demotion is legal."""
    uid = _seed("tenant-x", "solo", "admin")
    bystander = _member_of("tenant-x", "bystander")["user_id"]
    caller = member("tenant-x", "admin")
    assert http_status(tenant_routes.remove_tenant_member_route("tenant-x", uid, caller)) == 409
    demote = tenant_routes.change_tenant_member_role_route("tenant-x", uid, {"role": "viewer"}, caller)
    assert http_status(demote) == 409
    asyncio.run(tenant_routes.change_tenant_member_role_route("tenant-x", bystander, {"role": "admin"}, caller))
    out = asyncio.run(tenant_routes.change_tenant_member_role_route("tenant-x", uid, {"role": "viewer"}, caller))
    assert out["role"] == "viewer"


def test_set_member_role_join_failure_keeps_previous_role(fake_kc):
    """The new group is joined BEFORE the old ones are left, so a failed join never leaves zero roles."""
    uid = _seed("tenant-x", "carol")
    fake_kc.fail_join_groups.add(_group("tenant-x", "admin"))
    with pytest.raises(kc.KeycloakAdminError):
        kc.set_member_role("tenant-x", uid, "admin")
    assert uid in fake_kc.memberships[_group("tenant-x", "viewer")]
    assert kc.list_members("tenant-x")[0]["roles"] == ["viewer"]


def test_remove_from_group_accumulates_per_group_failures(fake_kc):
    """A failing detach must not abort the loop, and the caller learns which roles failed."""
    uid = _seed("tenant-x", "multi")
    _member_of("tenant-x", "multi", "admin")
    fake_kc.fail_delete_groups.add(_group("tenant-x", "viewer"))
    out = asyncio.run(tenant_routes.remove_tenant_member_route("tenant-x", uid, master_admin()))
    assert out["removed"] is False
    assert out["removed_roles"] == ["admin"]
    assert out["failed_roles"] == ["viewer"]
    assert uid not in fake_kc.memberships[_group("tenant-x", "admin")], "viewer (first) failing stopped the loop"
    assert all(isinstance(role, str) and "http" not in role for role in out["failed_roles"])


def test_member_route_errors_do_not_leak_keycloak_internals(fake_kc, monkeypatch):
    """A raw ``_admin_call`` error embeds the in-cluster admin URL and realm."""
    _tenant("tenant-x")

    def _boom(*_args, **_kwargs):
        raise kc.KeycloakAdminError("GET http://keycloak:8080/auth/admin/realms/docs-pipeline/groups -> 500: boom")

    monkeypatch.setattr(kc, "list_members", _boom)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(tenant_routes.list_tenant_members_route("tenant-x", member("tenant-x", "admin")))
    assert exc.value.status_code == 502
    for internal in ("keycloak:8080", "docs-pipeline", "boom"):
        assert internal not in exc.value.detail
