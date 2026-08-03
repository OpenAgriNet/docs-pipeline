"""Thin Keycloak Admin REST client for tenant / user provisioning.

This module lets the tenant-provisioning backend create the *identity-plane*
objects that back an app-side tenant: a Keycloak **Organization**, the per-tenant
``/<instance>`` group tree with its ``{admin, content_curator, viewer}`` role
children, and tenant-admin **users**. It complements the data-plane provisioning
(SQLite tenant registry + Marqo default index) that already lives in ``api.py``.

Design goals
------------
* **No new dependencies.** HTTP is done with ``urllib`` exactly like
  ``scripts/keycloak_bootstrap_docs_pipeline.py`` (the CLI companion that seeds a
  fresh realm). All requests funnel through :func:`_http_request` so tests can
  monkeypatch a single seam.
* **Inert when unconfigured.** The backend must keep running with KC admin turned
  off. If ``KEYCLOAK_ADMIN_CLIENT_SECRET`` is unset/empty every helper raises
  :class:`KeycloakAdminUnconfigured`, which the routes translate to a 503 (for the
  user/member endpoints) or a soft warning (for tenant creation).
* **Service account auth.** An admin bearer token is obtained via the OAuth2
  ``client_credentials`` grant using the ``KEYCLOAK_ADMIN_CLIENT_ID`` /
  ``KEYCLOAK_ADMIN_CLIENT_SECRET`` service account, cached until shortly before
  expiry.

Configuration (all read from the environment at call time)
----------------------------------------------------------
* ``KEYCLOAK_ADMIN_CLIENT_ID``     (default ``docs-pipeline-admin``)
* ``KEYCLOAK_ADMIN_CLIENT_SECRET`` (REQUIRED to enable; empty => inert)
* ``KEYCLOAK_ADMIN_BASE_URL``      (default ``http://keycloak:8080/auth``) — the
  in-cluster Keycloak root; Admin REST base becomes
  ``<base>/admin/realms/<realm>``.
* ``KEYCLOAK_REALM``               (default ``docs-pipeline``)
* ``KEYCLOAK_ISSUER``              browser-facing realm URL, e.g.
  ``https://sso.example.com/auth/realms/docs-pipeline``; the token endpoint is
  ``<issuer>/protocol/openid-connect/token``.
* ``KEYCLOAK_JWKS_URL``            used to *derive* an in-cluster token endpoint
  when the public issuer is unreachable from the backend (see
  :func:`_token_endpoints`).

Token-endpoint resolution
--------------------------
The issuer is the *browser-facing* URL and may be an HTTPS hostname the backend
cannot reach from inside the cluster. We therefore try, in order:

1. ``<KEYCLOAK_ISSUER>/protocol/openid-connect/token`` (if the issuer is set), and
2. a token endpoint derived from ``KEYCLOAK_JWKS_URL`` by swapping its trailing
   ``/certs`` for ``/token`` — the JWKS URL is the in-cluster address the token
   validator already uses, so it is reachable from the backend.

On a *connection* failure (DNS/refused/timeout) against candidate 1 we fall back
to candidate 2. An HTTP error (4xx/5xx) is a reachable-but-rejected response and
is surfaced immediately — we do not silently retry a bad credential elsewhere.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .auth.models import PLATFORM_ADMIN_ROLES

# Per-tenant roles, mirrored from the realm's group model.
ROLES = ("admin", "content_curator", "viewer")

# Realm-level roles that put an account OFF-LIMITS to tenant member management.
# The per-tenant guards only see ``/<instance>/<role>`` group paths, so without
# this a tenant admin could absorb a platform admin's account into its own group
# tree and then reset its password. Sourced from the auth model so the two can
# never drift.
PROTECTED_REALM_ROLES = frozenset(PLATFORM_ADMIN_ROLES | {"superadmin"})

# Well-known placeholder that used to ship in the realm export / compose defaults.
# It must NEVER authenticate: if the deployment still carries it, KC admin is
# treated as unconfigured (routes 503) so a public/guessable secret can't be used.
PLACEHOLDER_ADMIN_SECRET = "CHANGE_ME_ADMIN_SECRET"

_HTTP_TIMEOUT = 30

# Cached service-account token: {"access_token": str|None, "expires_at": float}
_TOKEN_CACHE: dict[str, Any] = {"access_token": None, "expires_at": 0.0}
# Refresh a bit before the real expiry to avoid using a token mid-flight.
_TOKEN_EXPIRY_SKEW = 30


class KeycloakAdminError(RuntimeError):
    """A Keycloak Admin API call failed (reachable server returned an error)."""


class KeycloakAdminForbidden(KeycloakAdminError):
    """The mutation targets an account tenant member management may not touch.

    Raised for a platform-admin account, an account that belongs to another
    tenant, or an existing account a non-platform caller tried to absorb into its
    own tenant. Routes translate this to an HTTP 403.
    """


class KeycloakAdminUnconfigured(KeycloakAdminError):
    """KC admin is not configured (no client secret) — the client is inert.

    Routes translate this to an HTTP 503 with a helpful message so the rest of
    the app keeps working without Keycloak admin credentials.
    """


# ---------------------------------------------------------------------------
# Configuration helpers (read env at call time so tests / redeploys can vary it)
# ---------------------------------------------------------------------------


def _admin_client_id() -> str:
    return (os.environ.get("KEYCLOAK_ADMIN_CLIENT_ID") or "docs-pipeline-admin").strip()


def _admin_client_secret() -> str:
    return (os.environ.get("KEYCLOAK_ADMIN_CLIENT_SECRET") or "").strip()


def _realm() -> str:
    return (os.environ.get("KEYCLOAK_REALM") or "docs-pipeline").strip() or "docs-pipeline"


def _admin_base_url() -> str:
    """Admin REST base: ``<KEYCLOAK_ADMIN_BASE_URL>/admin/realms/<realm>``."""
    root = (os.environ.get("KEYCLOAK_ADMIN_BASE_URL") or "http://keycloak:8080/auth").rstrip("/")
    return f"{root}/admin/realms/{_realm()}"


def _token_endpoints() -> list[str]:
    """Ordered token-endpoint candidates for the service-account (admin) token.

    The **admin-base host comes first**: Keycloak rejects an admin call (401)
    when the token's ``iss`` host differs from the host the Admin API is reached
    on. The service-account token is used only for admin calls, so mint it from
    the same host as ``KEYCLOAK_ADMIN_BASE_URL`` (typically the in-cluster
    ``http://keycloak:8080/auth``). Public ``KEYCLOAK_ISSUER`` / JWKS-derived
    endpoints follow as fallbacks. (User-JWT validation still uses the public
    issuer — that is unrelated to this token.)
    """
    candidates: list[str] = []

    admin_root = (os.environ.get("KEYCLOAK_ADMIN_BASE_URL") or "http://keycloak:8080/auth").rstrip("/")
    if admin_root:
        candidates.append(f"{admin_root}/realms/{_realm()}/protocol/openid-connect/token")

    issuer = (os.environ.get("KEYCLOAK_ISSUER") or "").rstrip("/")
    if issuer:
        endpoint = f"{issuer}/protocol/openid-connect/token"
        if endpoint not in candidates:
            candidates.append(endpoint)

    jwks = (os.environ.get("KEYCLOAK_JWKS_URL") or "").strip()
    if jwks:
        if jwks.endswith("/certs"):
            derived = jwks[: -len("/certs")] + "/token"
        elif "/protocol/openid-connect/" in jwks:
            base = jwks.split("/protocol/openid-connect/", 1)[0]
            derived = f"{base}/protocol/openid-connect/token"
        else:
            derived = None
        if derived and derived not in candidates:
            candidates.append(derived)

    return candidates


def is_configured() -> bool:
    """True when a *usable* client secret is present (KC admin can be used).

    A missing/empty secret — or the well-known :data:`PLACEHOLDER_ADMIN_SECRET`
    — counts as unconfigured so the placeholder can never authenticate against a
    real Keycloak.
    """
    secret = _admin_client_secret()
    return bool(secret) and secret != PLACEHOLDER_ADMIN_SECRET


def _require_configured() -> None:
    if not is_configured():
        raise KeycloakAdminUnconfigured(
            "Keycloak admin is not configured: set KEYCLOAK_ADMIN_CLIENT_SECRET "
            "to a real (non-placeholder) value (and KEYCLOAK_ADMIN_CLIENT_ID / "
            "KEYCLOAK_ADMIN_BASE_URL / KEYCLOAK_REALM) to enable tenant + user "
            "provisioning."
        )


# ---------------------------------------------------------------------------
# HTTP seam (single choke-point; monkeypatched in tests)
# ---------------------------------------------------------------------------


def _http_request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    body: Any = None,
    form: Optional[dict] = None,
    timeout: int = _HTTP_TIMEOUT,
) -> tuple[int, Any]:
    """Perform one HTTP request, mirroring the bootstrap script's ``_req``.

    Returns ``(status, parsed_json_or_None)``. A ``409 Conflict`` is treated as an
    idempotent no-op and returned as ``(409, None)``. Any other HTTP error is
    raised as :class:`KeycloakAdminError`. Connection-level failures propagate as
    ``urllib.error.URLError`` so the token resolver can fall back to another
    endpoint.
    """
    headers: dict[str, str] = {}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:  # reachable server, non-2xx
        raw = exc.read()
        detail = raw.decode("utf-8", errors="replace")
        if exc.code == 409:
            return exc.code, None
        raise KeycloakAdminError(f"{method} {url} -> {exc.code}: {detail}") from exc


# ---------------------------------------------------------------------------
# Service-account token (client_credentials, cached)
# ---------------------------------------------------------------------------


def reset_token_cache() -> None:
    """Drop any cached admin token (used by tests and after config changes)."""
    _TOKEN_CACHE["access_token"] = None
    _TOKEN_CACHE["expires_at"] = 0.0


def _admin_token() -> str:
    """Return a valid admin bearer token, fetching via client_credentials if needed.

    Cached until ``_TOKEN_EXPIRY_SKEW`` seconds before its expiry. Raises
    :class:`KeycloakAdminUnconfigured` when no client secret is set.
    """
    _require_configured()

    now = time.time()
    cached = _TOKEN_CACHE.get("access_token")
    if cached and now < float(_TOKEN_CACHE.get("expires_at") or 0.0):
        return cached

    endpoints = _token_endpoints()
    if not endpoints:
        raise KeycloakAdminError(
            "Cannot resolve a Keycloak token endpoint: set KEYCLOAK_ISSUER or "
            "KEYCLOAK_JWKS_URL."
        )

    form = {
        "grant_type": "client_credentials",
        "client_id": _admin_client_id(),
        "client_secret": _admin_client_secret(),
    }

    last_conn_error: Optional[Exception] = None
    for endpoint in endpoints:
        try:
            _status, payload = _http_request("POST", endpoint, form=form)
        except urllib.error.URLError as exc:  # connection failure -> try next
            last_conn_error = exc
            continue
        if not payload or not payload.get("access_token"):
            raise KeycloakAdminError(f"Token endpoint {endpoint} returned no access_token")
        token = payload["access_token"]
        expires_in = int(payload.get("expires_in") or 60)
        _TOKEN_CACHE["access_token"] = token
        _TOKEN_CACHE["expires_at"] = now + max(0, expires_in - _TOKEN_EXPIRY_SKEW)
        return token

    raise KeycloakAdminError(
        f"Could not reach any Keycloak token endpoint ({', '.join(endpoints)}): "
        f"{last_conn_error}"
    )


def _admin_call(method: str, path: str, *, body: Any = None) -> tuple[int, Any]:
    """Issue an authenticated Admin REST call. ``path`` is relative to the realm base."""
    token = _admin_token()
    url = f"{_admin_base_url()}{path}"
    return _http_request(method, url, token=token, body=body)


# ---------------------------------------------------------------------------
# Organizations (Keycloak 26)
# ---------------------------------------------------------------------------


def _list_organizations() -> list[dict]:
    _status, orgs = _admin_call("GET", "/organizations")
    return orgs or []


def list_organizations() -> list[dict]:
    """List the realm's Keycloak Organizations as ``[{name, id, alias, instance}]``.

    ``instance`` is the app-side tenant id the organization maps to: the
    ``instance`` custom attribute when present, else the org ``alias``/``name``
    (normalized). Used by the tenant reconcile to discover tenants that exist on
    the identity plane but have no app-side registry row yet.

    Raises :class:`KeycloakAdminUnconfigured` when KC admin has no client secret
    (the caller treats that as "no KC orgs, skip"). Other Admin errors propagate
    as :class:`KeycloakAdminError`.
    """
    _require_configured()
    result: list[dict] = []
    for org in _list_organizations():
        attrs = org.get("attributes") or {}
        inst_attr = attrs.get("instance")
        if isinstance(inst_attr, list):
            inst_attr = inst_attr[0] if inst_attr else None
        instance = (
            (inst_attr or org.get("alias") or org.get("name") or "").strip().lower() or None
        )
        result.append(
            {
                "name": org.get("name"),
                "id": org.get("id"),
                "alias": org.get("alias"),
                "instance": instance,
            }
        )
    return result


def ensure_organization(instance: str, display_name: Optional[str] = None) -> Optional[str]:
    """Ensure a Keycloak Organization exists for ``instance``; return its id.

    Idempotent by name. Tolerant of older realms that lack the Organizations
    endpoint (returns ``None`` rather than failing tenant creation).
    """
    inst = (instance or "").strip().lower()
    if not inst:
        raise KeycloakAdminError("instance is required")

    try:
        orgs = _list_organizations()
    except KeycloakAdminError:
        # Organizations unsupported/disabled on this realm — treat as optional.
        return None

    for org in orgs:
        if org.get("name") == inst or org.get("alias") == inst:
            return org.get("id")

    body = {
        "name": inst,
        "alias": inst,
        "enabled": True,
        "domains": [{"name": f"{inst}.example.com", "verified": False}],
        "attributes": {"instance": [inst]},
    }
    if display_name:
        body["attributes"]["display_name"] = [display_name]
    _admin_call("POST", "/organizations", body=body)

    for org in _list_organizations():
        if org.get("name") == inst or org.get("alias") == inst:
            return org.get("id")
    return None


def _add_org_member(org_id: str, user_id: str) -> None:
    # KC 26: POST the user id (as a bare JSON string) to the org members endpoint.
    _admin_call("POST", f"/organizations/{org_id}/members", body=user_id)


# ---------------------------------------------------------------------------
# Group tree: /<instance> with child role groups /<instance>/<role>
# ---------------------------------------------------------------------------


def _find_top_group(name: str) -> Optional[dict]:
    query = urllib.parse.urlencode({"search": name})
    _status, groups = _admin_call("GET", f"/groups?{query}")
    for group in groups or []:
        if group.get("name") == name:
            return group
    return None


def _find_child_group(parent_id: str, name: str) -> Optional[dict]:
    _status, children = _admin_call("GET", f"/groups/{parent_id}/children")
    for child in children or []:
        if child.get("name") == name:
            return child
    return None


def _ensure_top_group(name: str) -> str:
    existing = _find_top_group(name)
    if existing:
        return existing["id"]
    _admin_call("POST", "/groups", body={"name": name})
    created = _find_top_group(name)
    if not created:
        raise KeycloakAdminError(f"could not create/find group {name}")
    return created["id"]


def _ensure_child_group(parent_id: str, name: str) -> str:
    existing = _find_child_group(parent_id, name)
    if existing:
        return existing["id"]
    _admin_call("POST", f"/groups/{parent_id}/children", body={"name": name})
    created = _find_child_group(parent_id, name)
    if not created:
        raise KeycloakAdminError(f"could not create/find child group {name}")
    return created["id"]


def ensure_group_tree(instance: str) -> dict[str, str]:
    """Create ``/<instance>`` plus child role groups; return ``{path: group_id}``.

    Idempotent — reuses any groups that already exist.
    """
    inst = (instance or "").strip().lower()
    if not inst:
        raise KeycloakAdminError("instance is required")
    _require_configured()

    ids: dict[str, str] = {}
    top_id = _ensure_top_group(inst)
    ids[f"/{inst}"] = top_id
    for role in ROLES:
        ids[f"/{inst}/{role}"] = _ensure_child_group(top_id, role)
    return ids


def _resolve_group_tree(instance: str) -> dict[str, str]:
    """Read-only lookup of an existing ``/<instance>`` group tree ({path: id}).

    Unlike :func:`ensure_group_tree` this never creates groups; missing groups are
    simply absent from the result.
    """
    inst = (instance or "").strip().lower()
    ids: dict[str, str] = {}
    top = _find_top_group(inst)
    if not top:
        return ids
    ids[f"/{inst}"] = top["id"]
    _status, children = _admin_call("GET", f"/groups/{top['id']}/children")
    for child in children or []:
        if child.get("name") in ROLES:
            ids[f"/{inst}/{child['name']}"] = child["id"]
    return ids


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def _find_user_by_username(username: str) -> Optional[dict]:
    """Return the KC user whose username matches ``username`` exactly (case-insensitive).

    We pass ``exact=true`` to Keycloak, but still re-check the username locally and
    NEVER fall back to an arbitrary first result: a fuzzy match could resolve a
    different account (and then get its password reset / groups changed).
    """
    query = urllib.parse.urlencode({"username": username, "exact": "true"})
    _status, users = _admin_call("GET", f"/users?{query}")
    target = (username or "").strip().lower()
    for user in users or []:
        if (user.get("username") or "").strip().lower() == target:
            return user
    return None


def _derive_names(username: str, email: Optional[str]) -> tuple[str, str]:
    """Derive firstName/lastName — Keycloak 26 requires them (declarative profile)."""
    display = (username or "").replace("-", " ").replace("_", " ").strip().title()
    if not display:
        display = username or (email or "user")
    parts = display.split(" ", 1)
    first = parts[0] if parts and parts[0] else (username or "User")
    last = parts[1] if len(parts) > 1 and parts[1] else "User"
    return first, last


def create_user(
    username: str,
    email: Optional[str],
    temporary_password: str,
    group_path: str,
    *,
    add_org_membership: bool = True,
    allow_existing: bool = False,
) -> dict:
    """Create (or reuse) a user, join it to ``group_path``, set a temp password.

    * ``firstName`` / ``lastName`` are always set (KC26 requires them or login for
      a password grant fails with "account not fully set up").
    * ``emailVerified`` is set true so the tenant admin can log in immediately.
    * The password credential is written with ``temporary=true`` — the user is
      forced to change it on first login.
    * When ``add_org_membership`` is true, the user is also added as a member of
      the Organization matching the top segment of ``group_path`` (best-effort;
      tolerated if Organizations are unavailable).

    **Existing users are never hijacked.** ``_find_user_by_username`` is realm-wide
    with no tenant scoping, so absorbing an existing account into a tenant group is
    a platform-admin operation: it requires ``allow_existing=True`` (the routes set
    it only for a platform admin) and is additionally screened by
    :func:`assert_target_manageable`. Otherwise a tenant admin could name any
    account in the realm, join it to its own group tree — which grants that account
    the tenant's data — and then reset its password through the member routes.
    A permitted merge only *adds* the requested group membership: the existing user
    representation (``attributes`` / email / name) is left intact and the password
    is NOT reset. In that case the result is ``{"id", "username", "created": False}``
    with **no** ``temporary_password``. Only a newly-created user gets the temporary
    password set (and echoed back as ``temporary_password``).

    Returns ``{"id", "username", "created", ...}``.
    """
    _require_configured()
    uname = (username or "").strip()
    if not uname:
        raise KeycloakAdminError("username is required")
    path = (group_path or "").strip()
    if not path.startswith("/"):
        raise KeycloakAdminError("group_path must look like /<instance>/<role>")

    instance = path.strip("/").split("/")[0]

    existing = _find_user_by_username(uname)
    if existing:
        if not allow_existing:
            raise KeycloakAdminForbidden(
                f"user {uname} already exists in the realm; only a platform admin "
                "can add an existing account to a tenant"
            )
        # Merge-only: do NOT PUT-replace the rep (would clobber attributes / email /
        # name) and do NOT reset the password. We only add the group membership below.
        uid = existing["id"]
        # Screen the account even for a platform admin: a realm platform admin is
        # never re-homed into a tenant group tree.
        assert_target_manageable(instance, uid, platform_admin=True)
        created = False
    else:
        first, last = _derive_names(uname, email)
        rep = {
            "username": uname,
            "email": email or f"{uname}@{instance}.example.com",
            "emailVerified": True,
            "enabled": True,
            "firstName": first,
            "lastName": last,
            "attributes": {"instances": [instance], "envs": ["prod"]},
        }
        _admin_call("POST", "/users", body=rep)
        found = _find_user_by_username(uname)
        if not found:
            raise KeycloakAdminError(f"user {uname} was not found after creation")
        uid = found["id"]
        created = True
        # Password credential (temporary => must-change on first login). Set ONLY for
        # a freshly-created user; an existing user's password is never touched.
        _admin_call(
            "PUT",
            f"/users/{uid}/reset-password",
            body={"type": "password", "temporary": True, "value": temporary_password},
        )

    # Group membership (idempotent join). Resolve, creating the tree if absent.
    tree = _resolve_group_tree(instance)
    gid = tree.get(path)
    if not gid:
        tree = ensure_group_tree(instance)
        gid = tree.get(path)
    if not gid:
        raise KeycloakAdminError(f"group {path} not found for user {uname}")
    _admin_call("PUT", f"/users/{uid}/groups/{gid}", body={})

    # Optional Organization membership.
    if add_org_membership:
        try:
            org_id = ensure_organization(instance)
            if org_id:
                _add_org_member(org_id, uid)
        except KeycloakAdminError:
            # Org membership is best-effort; group membership already grants the role.
            pass

    result = {"id": uid, "username": uname, "created": created}
    if created:
        result["temporary_password"] = temporary_password
    return result


def list_members(instance: str) -> list[dict]:
    """List users in any ``/<instance>/*`` role group as ``[{username,email,roles}]``."""
    _require_configured()
    inst = (instance or "").strip().lower()
    tree = _resolve_group_tree(inst)

    by_username: dict[str, dict] = {}
    for role in ROLES:
        gid = tree.get(f"/{inst}/{role}")
        if not gid:
            continue
        _status, members = _admin_call("GET", f"/groups/{gid}/members")
        for member in members or []:
            uname = member.get("username") or member.get("id")
            entry = by_username.setdefault(
                uname,
                {
                    # ``user_id`` is what the per-member mutation routes
                    # (remove / change-role / reset-password) address, so it
                    # must be echoed alongside the display fields.
                    "user_id": member.get("id"),
                    "username": uname,
                    "email": member.get("email"),
                    "roles": [],
                },
            )
            if entry.get("user_id") is None and member.get("id"):
                entry["user_id"] = member.get("id")
            if entry.get("email") is None and member.get("email"):
                entry["email"] = member.get("email")
            if role not in entry["roles"]:
                entry["roles"].append(role)

    return sorted(by_username.values(), key=lambda m: (m.get("username") or ""))


def _member_role_groups(instance: str, user_id: str) -> dict[str, str]:
    """Return ``{role: group_id}`` for the tenant role groups ``user_id`` is in.

    Reads each ``/<instance>/<role>`` group's membership and keeps only the roles
    the user actually holds. An empty result means the user is **not** a member of
    this tenant (which the routes translate into a 404, so a tenant admin can
    never remove / re-role / reset a user who lives in another tenant).
    """
    inst = (instance or "").strip().lower()
    tree = _resolve_group_tree(inst)
    held: dict[str, str] = {}
    for role in ROLES:
        gid = tree.get(f"/{inst}/{role}")
        if not gid:
            continue
        _status, members = _admin_call("GET", f"/groups/{gid}/members")
        for member in members or []:
            if member.get("id") == user_id:
                held[role] = gid
                break
    return held


def _user_realm_roles(user_id: str) -> set[str]:
    """Realm roles held by ``user_id`` (lowercased).

    ``_member_role_groups`` only sees group paths, so realm roles — the ones that
    make an account a platform admin — are invisible to it. This is the read that
    lets the guards below refuse a mutation on such an account.
    """
    _status, roles = _admin_call("GET", f"/users/{user_id}/role-mappings/realm")
    return {(r.get("name") or "").strip().lower() for r in roles or [] if r.get("name")}


def _user_tenant_instances(user_id: str) -> set[str]:
    """Tenant instance ids ``user_id`` is a member of (top segment of each group path)."""
    _status, groups = _admin_call("GET", f"/users/{user_id}/groups")
    instances: set[str] = set()
    for group in groups or []:
        path = (group.get("path") or "").strip("/")
        if path:
            instances.add(path.split("/")[0].strip().lower())
    return instances


def assert_target_manageable(instance: str, user_id: str, *, platform_admin: bool = False) -> None:
    """Refuse a member mutation that targets an account the caller may not touch.

    Two checks, both invisible to :func:`_member_role_groups`:

    * the target holds a realm role in :data:`PROTECTED_REALM_ROLES` — refused for
      **everyone**, so a platform admin's account can never be re-homed, re-roled
      or password-reset through the tenant member surface;
    * the target is also a member of some OTHER tenant — refused unless the caller
      is a platform admin, so a tenant admin can never reach across tenants.

    Raises :class:`KeycloakAdminForbidden`; returns ``None`` when allowed.
    """
    inst = (instance or "").strip().lower()
    protected = _user_realm_roles(user_id) & PROTECTED_REALM_ROLES
    if protected:
        raise KeycloakAdminForbidden(
            "target account holds a platform-admin realm role and cannot be "
            "managed through tenant member management"
        )
    if platform_admin:
        return
    others = _user_tenant_instances(user_id) - {inst}
    if others:
        raise KeycloakAdminForbidden(
            "target account belongs to another tenant; only a platform admin can "
            "manage it"
        )


def remove_from_group(instance: str, user_id: str, *, platform_admin: bool = False) -> dict:
    """Remove ``user_id`` from **every** ``/<instance>/*`` role group.

    This detaches the user from the tenant entirely (all its roles). Returns
    ``{user_id, removed_roles, failed_roles, was_member}``; ``was_member`` is
    ``False`` when the user held no role in the tenant (nothing removed) so the
    route can 404.

    Each group detach is attempted independently and failures are **accumulated**
    into ``failed_roles`` (``[{role, error}]``) instead of aborting the loop: a
    raise partway through would leave the user half-removed with the caller told
    only that the request 502'd.
    """
    _require_configured()
    held = _member_role_groups(instance, user_id)
    if not held:
        return {"user_id": user_id, "removed_roles": [], "failed_roles": [], "was_member": False}
    assert_target_manageable(instance, user_id, platform_admin=platform_admin)

    removed: list[str] = []
    failed: list[dict] = []
    for role, gid in sorted(held.items()):
        try:
            _admin_call("DELETE", f"/users/{user_id}/groups/{gid}")
            removed.append(role)
        except KeycloakAdminError as exc:
            failed.append({"role": role, "error": str(exc)})
    return {
        "user_id": user_id,
        "removed_roles": removed,
        "failed_roles": failed,
        "was_member": True,
    }


def set_member_role(instance: str, user_id: str, role: str, *, platform_admin: bool = False) -> dict:
    """Set ``user_id``'s tenant membership to **exactly** ``role``.

    The user must already be a member of the tenant (else ``was_member=False`` and
    no change is made — the route 404s). ``role`` must be one of :data:`ROLES`;
    platform roles can never be assigned here. Other tenant role groups the user
    was in are removed so a member holds a single role. Returns
    ``{user_id, role, previous_roles, was_member}``.

    The target group is joined **before** the other role groups are dropped: the
    reverse order leaves a user with zero roles (silently ejected from the tenant,
    and a lockout if they were its last admin) whenever the join fails.
    """
    _require_configured()
    inst = (instance or "").strip().lower()
    target = (role or "").strip().lower()
    if target not in ROLES:
        raise KeycloakAdminError(f"role must be one of {', '.join(ROLES)}")

    held = _member_role_groups(inst, user_id)
    if not held:
        return {"user_id": user_id, "role": target, "previous_roles": [], "was_member": False}
    assert_target_manageable(inst, user_id, platform_admin=platform_admin)

    tree = _resolve_group_tree(inst)
    target_gid = tree.get(f"/{inst}/{target}")
    if not target_gid:
        tree = ensure_group_tree(inst)
        target_gid = tree.get(f"/{inst}/{target}")
    if not target_gid:
        raise KeycloakAdminError(f"group /{inst}/{target} not found")

    # Idempotent join to the target role group FIRST, so a failure here leaves the
    # member's existing roles intact rather than stripping them to nothing.
    _admin_call("PUT", f"/users/{user_id}/groups/{target_gid}", body={})
    # Only then drop every other role the user currently holds in this tenant.
    for held_role, gid in held.items():
        if held_role != target:
            _admin_call("DELETE", f"/users/{user_id}/groups/{gid}")

    return {
        "user_id": user_id,
        "role": target,
        "previous_roles": sorted(held.keys()),
        "was_member": True,
    }


def reset_password(instance: str, user_id: str, *, platform_admin: bool = False) -> dict:
    """Reset a tenant member's password to a fresh temporary one.

    The user must be a member of ``instance`` (else ``was_member=False`` — the
    route 404s — so a tenant admin cannot reset a foreign user's password). The
    new password is ``temporary=true`` (must-change on first login) and is echoed
    once as ``temporary_password``. Returns
    ``{user_id, temporary_password, was_member}``.
    """
    _require_configured()
    held = _member_role_groups(instance, user_id)
    if not held:
        return {"user_id": user_id, "temporary_password": None, "was_member": False}
    assert_target_manageable(instance, user_id, platform_admin=platform_admin)
    temporary_password = generate_temporary_password()
    _admin_call(
        "PUT",
        f"/users/{user_id}/reset-password",
        body={"type": "password", "temporary": True, "value": temporary_password},
    )
    return {
        "user_id": user_id,
        "temporary_password": temporary_password,
        "was_member": True,
    }


# ---------------------------------------------------------------------------
# Password generation
# ---------------------------------------------------------------------------


def generate_temporary_password(length: int = 20) -> str:
    """Generate a strong random temporary password (upper/lower/digit/symbol)."""
    length = max(16, length)
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pwd)
            and any(c.isupper() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!@#$%^&*-_" for c in pwd)
        ):
            return pwd
