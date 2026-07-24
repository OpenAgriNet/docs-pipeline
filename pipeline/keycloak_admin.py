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

# Per-tenant roles, mirrored from the realm's group model.
ROLES = ("admin", "content_curator", "viewer")

_HTTP_TIMEOUT = 30

# Cached service-account token: {"access_token": str|None, "expires_at": float}
_TOKEN_CACHE: dict[str, Any] = {"access_token": None, "expires_at": 0.0}
# Refresh a bit before the real expiry to avoid using a token mid-flight.
_TOKEN_EXPIRY_SKEW = 30


class KeycloakAdminError(RuntimeError):
    """A Keycloak Admin API call failed (reachable server returned an error)."""


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
    """Ordered token-endpoint candidates (issuer first, JWKS-derived fallback)."""
    candidates: list[str] = []

    issuer = (os.environ.get("KEYCLOAK_ISSUER") or "").rstrip("/")
    if issuer:
        candidates.append(f"{issuer}/protocol/openid-connect/token")

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
    """True when a client secret is present (KC admin can be used)."""
    return bool(_admin_client_secret())


def _require_configured() -> None:
    if not _admin_client_secret():
        raise KeycloakAdminUnconfigured(
            "Keycloak admin is not configured: set KEYCLOAK_ADMIN_CLIENT_SECRET "
            "(and KEYCLOAK_ADMIN_CLIENT_ID / KEYCLOAK_ADMIN_BASE_URL / "
            "KEYCLOAK_REALM) to enable tenant + user provisioning."
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
    query = urllib.parse.urlencode({"username": username, "exact": "true"})
    _status, users = _admin_call("GET", f"/users?{query}")
    for user in users or []:
        if (user.get("username") or "").lower() == username.lower():
            return user
    return (users or [None])[0] if users else None


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

    Returns ``{"id", "username", "created"}``.
    """
    _require_configured()
    uname = (username or "").strip()
    if not uname:
        raise KeycloakAdminError("username is required")
    path = (group_path or "").strip()
    if not path.startswith("/"):
        raise KeycloakAdminError("group_path must look like /<instance>/<role>")

    instance = path.strip("/").split("/")[0]
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

    existing = _find_user_by_username(uname)
    created = False
    if existing:
        uid = existing["id"]
        _admin_call("PUT", f"/users/{uid}", body=rep)
    else:
        _admin_call("POST", "/users", body=rep)
        found = _find_user_by_username(uname)
        if not found:
            raise KeycloakAdminError(f"user {uname} was not found after creation")
        uid = found["id"]
        created = True

    # Password credential (temporary => must-change on first login).
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

    return {"id": uid, "username": uname, "created": created}


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
                {"username": uname, "email": member.get("email"), "roles": []},
            )
            if entry.get("email") is None and member.get("email"):
                entry["email"] = member.get("email")
            if role not in entry["roles"]:
                entry["roles"].append(role)

    return sorted(by_username.values(), key=lambda m: (m.get("username") or ""))


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
