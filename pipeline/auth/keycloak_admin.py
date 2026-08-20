"""Keycloak Admin API helper for super-admin user provisioning (Plan 2).

Creates/updates users and assigns groups/roles in the same realm used for SSO.
Requires admin credentials via env (see ENV.md). Does not store access in the app DB.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from .groups import (
    ROLE_BH_VIEWER,
    ROLE_STATE_ADMIN,
    ROLE_STATE_APPROVER,
    ROLE_STATE_CONTRIBUTOR,
    ROLE_STATE_VIEW,
    ROLE_SUPER_ADMIN,
    STATE_ROLES,
    group_leaf_for_role,
    normalize_role,
)
from .tenancy import PORTAL_INSTANCE

# Realm-role descriptions written when a role is auto-created.
STATE_ROLE_DESCRIPTIONS: dict[str, str] = {
    ROLE_STATE_ADMIN: "State admin — upload, edit, review & approve, pipeline, delete own",
    ROLE_STATE_APPROVER: "State approver — upload, edit, review & approve, pipeline",
    ROLE_STATE_CONTRIBUTOR: "State contributor — upload and run pipeline only",
    ROLE_STATE_VIEW: "State view — read-only access in assigned state",
}

# Global access types accepted by ``ensure_access_group``.
_SUPER_ADMIN_ACCESS_TYPES = ("super_admin", "super-admin", "bv", "portal")
_BH_VIEWER_ACCESS_TYPES = ("bh_viewer", "bh-viewer", "bh_view")

# Common state codes for the admin UI picker (Keycloak groups created on demand).
DEFAULT_STATE_CODES = [
    "MH",
    "BH",
    "UP",
    "GJ",
    "RJ",
    "MP",
    "KA",
    "TS",
    "AP",
    "TN",
    "WB",
    "OR",
    "PB",
    "HR",
    "KL",
    "AS",
    "JH",
    "CG",
    "UK",
    "HP",
    "GA",
    "DL",
    "JK",
    "LA",
]


@dataclass
class KeycloakAdminConfig:
    base_url: str
    realm: str
    admin_username: str
    admin_password: str
    token_realm: str = "master"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.realm and self.admin_username and self.admin_password)


def load_keycloak_admin_config() -> KeycloakAdminConfig:
    """Load admin settings; derive base/realm from KEYCLOAK_ISSUER when possible."""
    issuer = (os.environ.get("KEYCLOAK_ISSUER") or "").strip().rstrip("/")
    base = (os.environ.get("KEYCLOAK_ADMIN_BASE_URL") or "").strip().rstrip("/")
    realm = (os.environ.get("KEYCLOAK_ADMIN_REALM") or "").strip()

    if issuer and "/realms/" in issuer:
        # https://host/auth/realms/bharat-vistaar
        head, _, realm_part = issuer.partition("/realms/")
        if not base:
            base = head
        if not realm:
            realm = realm_part.split("/")[0]

    username = (
        os.environ.get("KEYCLOAK_ADMIN_USERNAME")
        or os.environ.get("KEYCLOAK_ADMIN")
        or ""
    ).strip()
    password = (os.environ.get("KEYCLOAK_ADMIN_PASSWORD") or "").strip()
    token_realm = (os.environ.get("KEYCLOAK_ADMIN_TOKEN_REALM") or "master").strip() or "master"

    return KeycloakAdminConfig(
        base_url=base,
        realm=realm or "bharat-vistaar",
        admin_username=username,
        admin_password=password,
        token_realm=token_realm,
    )


def _req(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Any = None,
    form: dict | None = None,
) -> tuple[int, Any]:
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
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = response.read()
            if not raw:
                return response.status, None
            return response.status, json.loads(raw.decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw
        raise HTTPException(exc.code if 400 <= exc.code < 600 else 502, detail) from exc
    except urllib.error.URLError as exc:
        raise HTTPException(502, f"Keycloak admin unreachable: {exc}") from exc


def _admin_token(cfg: KeycloakAdminConfig) -> str:
    status, body = _req(
        "POST",
        f"{cfg.base_url}/realms/{cfg.token_realm}/protocol/openid-connect/token",
        form={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": cfg.admin_username,
            "password": cfg.admin_password,
        },
    )
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        raise HTTPException(502, "Unable to obtain Keycloak admin token")
    return str(body["access_token"])


def _admin_root(cfg: KeycloakAdminConfig) -> str:
    return f"{cfg.base_url}/admin/realms/{cfg.realm}"


def require_admin_config() -> KeycloakAdminConfig:
    cfg = load_keycloak_admin_config()
    if not cfg.configured:
        raise HTTPException(
            503,
            "Keycloak admin is not configured. Set KEYCLOAK_ADMIN_USERNAME and "
            "KEYCLOAK_ADMIN_PASSWORD (and KEYCLOAK_ISSUER or KEYCLOAK_ADMIN_BASE_URL / "
            "KEYCLOAK_ADMIN_REALM).",
        )
    return cfg


def _ensure_realm_role(admin: str, token: str, name: str, description: str = "") -> dict:
    try:
        status, role = _req("GET", f"{admin}/roles/{urllib.parse.quote(name)}", token=token)
        if status == 200 and isinstance(role, dict):
            return role
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        # Role missing — create below.
    try:
        _req(
            "POST",
            f"{admin}/roles",
            token=token,
            body={"name": name, "description": description or name},
        )
    except HTTPException as exc:
        # 409 conflict = already created concurrently; re-get below.
        if exc.status_code not in (409,):
            raise
    try:
        status, role = _req("GET", f"{admin}/roles/{urllib.parse.quote(name)}", token=token)
    except HTTPException as exc:
        raise HTTPException(502, f"Unable to ensure realm role {name}: {exc.detail}") from exc
    if status != 200 or not isinstance(role, dict):
        raise HTTPException(502, f"Unable to ensure realm role {name}")
    return role


def _get_group(admin: str, token: str, group_id: str) -> dict:
    status, group = _req("GET", f"{admin}/groups/{group_id}", token=token)
    if status != 200 or not isinstance(group, dict):
        raise HTTPException(502, "Unable to load Keycloak group")
    return group


def _get_children(admin: str, token: str, parent_id: str) -> list[dict]:
    """List a group's direct children via the dedicated endpoint.

    Not via ``parent.get("subGroups")`` on the parent's own representation —
    on this Keycloak version that field comes back empty/unreliable even when
    children genuinely exist (a known API change: subgroup listing moved to
    ``GET /groups/{id}/children``). Trusting the embedded field caused real
    child groups created by realm import to look "missing", so a create was
    attempted, hit 409 (already exists), and then verification failed the
    same way — surfacing as "Failed to create child group <name>" even
    though the group was there all along.
    """
    status, children = _req("GET", f"{admin}/groups/{parent_id}/children?max=200", token=token)
    return children if isinstance(children, list) else []


def _find_child(admin: str, token: str, parent_id: str, name: str) -> dict | None:
    for child in _get_children(admin, token, parent_id):
        if isinstance(child, dict) and child.get("name") == name:
            return child
    return None


def _ensure_child_group(admin: str, token: str, parent_id: str | None, name: str) -> dict:
    if parent_id is None:
        status, top = _req("GET", f"{admin}/groups?max=200", token=token)
        groups = top if isinstance(top, list) else []
        for g in groups:
            if g.get("name") == name:
                return _get_group(admin, token, g["id"])
        try:
            _req("POST", f"{admin}/groups", token=token, body={"name": name})
        except HTTPException as exc:
            if exc.status_code not in (409,):
                raise
        status, top = _req("GET", f"{admin}/groups?max=200", token=token)
        for g in top or []:
            if g.get("name") == name:
                return _get_group(admin, token, g["id"])
        raise HTTPException(502, f"Failed to create group {name}")

    found = _find_child(admin, token, parent_id, name)
    if found:
        return _get_group(admin, token, found["id"])
    try:
        _req("POST", f"{admin}/groups/{parent_id}/children", token=token, body={"name": name})
    except HTTPException as exc:
        if exc.status_code not in (409,):
            raise
    found = _find_child(admin, token, parent_id, name)
    if not found:
        raise HTTPException(502, f"Failed to create child group {name}")
    return _get_group(admin, token, found["id"])


def _map_role_on_group(admin: str, token: str, group_id: str, role: dict) -> None:
    status, existing = _req("GET", f"{admin}/groups/{group_id}/role-mappings/realm", token=token)
    names = {r.get("name") for r in (existing or []) if isinstance(r, dict)}
    if role["name"] in names:
        return
    _req(
        "POST",
        f"{admin}/groups/{group_id}/role-mappings/realm",
        token=token,
        body=[{"id": role["id"], "name": role["name"]}],
    )


def ensure_access_group(
    cfg: KeycloakAdminConfig,
    *,
    access_type: str,
    state: str | None,
    role: str | None,
) -> tuple[str, dict]:
    """Ensure group exists; return (path, group_rep)."""
    token = _admin_token(cfg)
    admin = _admin_root(cfg)
    access_type = (access_type or "").strip().lower()

    if access_type in _BH_VIEWER_ACCESS_TYPES:
        viewer_role = _ensure_realm_role(
            admin, token, ROLE_BH_VIEWER, "Bharat Vistaar viewer — all states, read-only"
        )
        global_g = _ensure_child_group(admin, token, None, "global")
        leaf = _ensure_child_group(admin, token, global_g["id"], "bh-viewer")
        leaf_full = _get_group(admin, token, leaf["id"])
        _map_role_on_group(admin, token, leaf_full["id"], viewer_role)
        return leaf_full.get("path") or "/global/bh-viewer", leaf_full

    if access_type in _SUPER_ADMIN_ACCESS_TYPES:
        sa_role = _ensure_realm_role(
            admin, token, ROLE_SUPER_ADMIN, "Platform super admin — all states"
        )
        global_g = _ensure_child_group(admin, token, None, "global")
        # Prefer nested super-admin; also accept flat name used in some installs
        leaf = _find_child(admin, token, global_g["id"], "super-admin")
        if not leaf:
            # Flat path group named global/super-admin
            status, top = _req("GET", f"{admin}/groups?max=200", token=token)
            for g in top or []:
                if g.get("path") == "/global/super-admin" or g.get("name") == "global/super-admin":
                    leaf_full = _get_group(admin, token, g["id"])
                    _map_role_on_group(admin, token, leaf_full["id"], sa_role)
                    return leaf_full.get("path") or "/global/super-admin", leaf_full
            leaf = _ensure_child_group(admin, token, global_g["id"], "super-admin")
        leaf_full = _get_group(admin, token, leaf["id"] if isinstance(leaf, dict) else leaf)
        _map_role_on_group(admin, token, leaf_full["id"], sa_role)
        return leaf_full.get("path") or "/global/super-admin", leaf_full

    state_code = (state or "").strip().upper()
    role_canon = normalize_role(role) or (role or "").strip().lower()
    if not state_code or not re.fullmatch(r"[A-Z0-9]{2,5}", state_code):
        raise HTTPException(400, "state must be a 2–5 letter code (e.g. MH, UP)")
    if role_canon not in STATE_ROLES:
        raise HTTPException(
            400,
            "role must be one of: " + ", ".join(STATE_ROLES),
        )

    realm_role = _ensure_realm_role(
        admin,
        token,
        role_canon,
        STATE_ROLE_DESCRIPTIONS[role_canon],
    )
    # Group leaf: /states/MH/admin or /states/MH/view
    leaf_name = group_leaf_for_role(role_canon)
    states = _ensure_child_group(admin, token, None, "states")
    state_g = _ensure_child_group(admin, token, states["id"], state_code)
    leaf = _ensure_child_group(admin, token, state_g["id"], leaf_name)
    _map_role_on_group(admin, token, leaf["id"], realm_role)
    path = leaf.get("path") or f"/states/{state_code}/{leaf_name}"
    return path, leaf


def _username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].strip().lower()
    local = re.sub(r"[^a-z0-9._-]+", "", local) or "user"
    return local[:64]


def provision_user(
    *,
    email: str,
    first_name: str = "",
    last_name: str = "",
    username: str | None = None,
    access_type: str,
    state: str | None = None,
    role: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """Create or update Keycloak user and join the access group."""
    cfg = require_admin_config()
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email is required (used for Google SSO)")

    group_path, group = ensure_access_group(
        cfg, access_type=access_type, state=state, role=role
    )
    token = _admin_token(cfg)
    admin = _admin_root(cfg)

    uname = (username or "").strip() or _username_from_email(email)
    # Find by email
    status, found = _req(
        "GET",
        f"{admin}/users?{urllib.parse.urlencode({'email': email, 'exact': 'true'})}",
        token=token,
    )
    body = {
        "username": uname,
        "email": email,
        "enabled": bool(enabled),
        "emailVerified": True,
        "firstName": (first_name or "").strip() or None,
        "lastName": (last_name or "").strip() or None,
        "requiredActions": [],
    }
    # Keycloak rejects null names sometimes — omit empty
    body = {k: v for k, v in body.items() if v is not None}

    created = False
    if isinstance(found, list) and found:
        uid = found[0]["id"]
        body["username"] = found[0].get("username") or uname
        _req("PUT", f"{admin}/users/{uid}", token=token, body=body)
    else:
        try:
            _req("POST", f"{admin}/users", token=token, body=body)
            created = True
        except HTTPException as exc:
            if exc.status_code != 409:
                raise
        status, found = _req(
            "GET",
            f"{admin}/users?{urllib.parse.urlencode({'email': email, 'exact': 'true'})}",
            token=token,
        )
        if not found:
            status, found = _req(
                "GET",
                f"{admin}/users?{urllib.parse.urlencode({'username': uname, 'exact': 'true'})}",
                token=token,
            )
        if not found:
            raise HTTPException(502, "User created but could not be loaded")
        uid = found[0]["id"]

    # Join group
    try:
        _req("PUT", f"{admin}/users/{uid}/groups/{group['id']}", token=token)
    except HTTPException:
        raise

    # Group membership is the ONLY source of roles. The realm role is already
    # mapped onto the leaf group by ``ensure_access_group``, so it reaches the
    # JWT through the group. Deliberately no direct user→role mapping here:
    # direct assignments produce users who carry permissions with no tenant
    # scope, and they are invisible in the group tree.
    access_key = (access_type or "").lower()
    if access_key in _SUPER_ADMIN_ACCESS_TYPES:
        role_name = ROLE_SUPER_ADMIN
    elif access_key in _BH_VIEWER_ACCESS_TYPES:
        role_name = ROLE_BH_VIEWER
    else:
        role_name = normalize_role(role) or (role or "").strip().lower()

    status, user = _req("GET", f"{admin}/users/{uid}", token=token)
    status, groups = _req("GET", f"{admin}/users/{uid}/groups", token=token)
    status, roles = _req("GET", f"{admin}/users/{uid}/role-mappings/realm", token=token)

    ui_url = (os.environ.get("DOCS_PIPELINE_UI_URL") or "http://localhost:3001").rstrip("/")
    login_url = f"{ui_url}/login" if not ui_url.endswith("/login") else ui_url

    if role_name == ROLE_SUPER_ADMIN:
        access_label = "Super Admin (Bharat Vistaar — all states)"
    elif role_name == ROLE_BH_VIEWER:
        access_label = "BH Viewer (Bharat Vistaar — all states, read-only)"
    else:
        access_label = f"{(state or '').upper()} · {(role or '').capitalize()}"

    share_lines = [
        "You have been given access to the Docs Pipeline console.",
        "",
        f"App URL: {ui_url}",
        f"Login page: {login_url}",
        "Sign-in: Continue with SSO (Google)",
        f"Use this Google account email: {email}",
        "",
        f"Access level: {access_label}",
        f"Keycloak group: {group_path}",
        "",
        "Steps:",
        "1. Open the app URL",
        "2. Click Continue with SSO",
        "3. Choose the Google account with the email above",
        "4. After first login, you should land on the dashboard",
        "",
        "If sign-in fails, contact your platform administrator.",
    ]

    return {
        "created": created,
        "user_id": uid,
        "username": (user or {}).get("username") or uname,
        "email": email,
        "first_name": (user or {}).get("firstName") or first_name,
        "last_name": (user or {}).get("lastName") or last_name,
        "enabled": bool((user or {}).get("enabled", enabled)),
        "email_verified": bool((user or {}).get("emailVerified", True)),
        "access_type": (
            "super_admin"
            if role_name == ROLE_SUPER_ADMIN
            else "bh_viewer"
            if role_name == ROLE_BH_VIEWER
            else "state"
        ),
        "state": (state or "").upper() or None,
        "role": role_name,
        "group_path": group_path,
        "groups": [g.get("path") for g in (groups or []) if isinstance(g, dict)],
        "roles": [r.get("name") for r in (roles or []) if isinstance(r, dict)],
        "app_url": ui_url,
        "login_url": login_url,
        "portal_instance": PORTAL_INSTANCE,
        "share_message": "\n".join(share_lines),
    }


def list_access_options() -> dict[str, Any]:
    """Static options for the admin form (no Keycloak call required)."""
    cfg = load_keycloak_admin_config()
    return {
        "keycloak_admin_configured": cfg.configured,
        "realm": cfg.realm,
        "portal_instance": PORTAL_INSTANCE,
        "access_types": [
            {
                "id": "super_admin",
                "label": "Super Admin (Bharat Vistaar)",
                "description": "Full access to all states, settings, user management, and PROD approval",
                "group": "/global/super-admin",
            },
            {
                "id": "bh_viewer",
                "label": "BH Viewer (Bharat Vistaar)",
                "description": "Read-only access across all states. No upload, edit, or approve.",
                "group": "/global/bh-viewer",
            },
            {
                "id": "state",
                "label": "State / Centre role",
                "description": (
                    "Access limited to one state or centre, as Admin, Approver, "
                    "Contributor, or View"
                ),
                "group_template": "/states/{STATE}/{admin|approver|contributor|view}",
            },
        ],
        "state_roles": [
            {
                "id": ROLE_STATE_ADMIN,
                "label": "Admin",
                "description": (
                    "Everything in the state: upload, edit, review & approve, "
                    "run pipeline, delete own documents"
                ),
            },
            {
                "id": ROLE_STATE_APPROVER,
                "label": "Approver",
                "description": (
                    "Upload, edit, review & approve, run pipeline. Cannot delete."
                ),
            },
            {
                "id": ROLE_STATE_CONTRIBUTOR,
                "label": "Contributor",
                "description": (
                    "Upload and run pipeline. Cannot edit, approve, or delete."
                ),
            },
            {
                "id": ROLE_STATE_VIEW,
                "label": "View",
                "description": "View-only access within the state (search / browse)",
            },
        ],
        "states": [{"code": c, "label": c} for c in DEFAULT_STATE_CODES],
        "required_fields": {
            "super_admin": ["email", "first_name", "last_name"],
            "bh_viewer": ["email", "first_name", "last_name"],
            "state": ["email", "first_name", "last_name", "state", "role"],
        },
        "notes": [
            "Users sign in with Google SSO using the same email you enter.",
            "No app password is required for normal SSO login.",
            "State codes must match document instance tags (mh, up, …).",
            "A centre is modelled as just another state code.",
            "Super admin (BV) documents can use portal instance 'bv'.",
            "Access comes from group membership only — no direct role assignment.",
            "Keycloak groups: /global/super-admin, /global/bh-viewer, "
            "/states/{STATE}/{admin|approver|contributor|view}",
        ],
    }


def _is_product_group_path(path: str) -> bool:
    """True when a group path grants a product role.

    ``/global/super-admin``, ``/global/bh-viewer``, or ``/states/{CODE}/{leaf}``.
    Anything else (org units, ad-hoc groups) is left alone when changing a role.
    """
    clean = (path or "").rstrip("/")
    if clean in ("/global/super-admin", "/global/bh-viewer"):
        return True
    parts = [p for p in clean.split("/") if p]
    return len(parts) == 3 and parts[0].lower() == "states"


def set_user_access(
    *,
    user_id: str,
    access_type: str,
    state: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Move an existing user to exactly one product role.

    A user holds at most ONE product group: the target is joined and every
    other product group is left, so roles never stack. Direct realm-role
    assignments are stripped too — group membership is the only source of
    access, and a leftover direct role would silently outrank the new group.

    The change only reaches the user's JWT on their next login.
    """
    cfg = require_admin_config()
    token = _admin_token(cfg)
    admin = _admin_root(cfg)

    status, target_user = _req("GET", f"{admin}/users/{user_id}", token=token)
    if status != 200 or not target_user:
        raise HTTPException(404, "User not found")

    path, group = ensure_access_group(cfg, access_type=access_type, state=state, role=role)

    _req("PUT", f"{admin}/users/{user_id}/groups/{group['id']}", token=token)

    # Exactly one product group.
    _, current = _req("GET", f"{admin}/users/{user_id}/groups", token=token)
    removed: list[str] = []
    for existing in current or []:
        existing_path = existing.get("path") or ""
        if existing_path.rstrip("/") == path.rstrip("/"):
            continue
        if not _is_product_group_path(existing_path):
            continue
        _req("DELETE", f"{admin}/users/{user_id}/groups/{existing['id']}", token=token)
        removed.append(existing_path)

    # Groups are the only source of roles.
    _, mapped = _req("GET", f"{admin}/users/{user_id}/role-mappings/realm", token=token)
    stripped: list[str] = []
    for realm_role in mapped or []:
        name = realm_role.get("name") or ""
        if name in ("offline_access", "uma_authorization") or name.startswith("default-roles"):
            continue
        _req(
            "DELETE",
            f"{admin}/users/{user_id}/role-mappings/realm",
            token=token,
            body=[{"id": realm_role["id"], "name": name}],
        )
        stripped.append(name)

    _, after = _req("GET", f"{admin}/users/{user_id}/groups", token=token)
    paths = [g.get("path") or "" for g in (after or [])]
    summary = _access_summary_from_groups(paths)

    return {
        "user_id": user_id,
        "email": target_user.get("email") or "",
        "username": target_user.get("username") or "",
        "group": path,
        "groups": paths,
        "removed_groups": removed,
        "removed_direct_roles": stripped,
        **summary,
        "requires_relogin": True,
        "note": "The user must sign out and back in for the new role to take effect.",
    }


def _access_summary_from_groups(group_paths: list[str]) -> dict[str, Any]:
    """Derive display access from Keycloak group paths.

    Users without product groups (super_admin / state roles) are labeled
    **Dashboard** — baseline SSO access (search/view only), not an error state.
    """
    paths = [p or "" for p in group_paths]
    if any(
        "/global/super-admin" in p
        or p.rstrip("/").endswith("/super-admin")
        or p.rstrip("/").endswith("global/super-admin")
        for p in paths
    ):
        return {
            "access_type": "super_admin",
            "access_label": "Super Admin",
            "states": [],
            "roles": [ROLE_SUPER_ADMIN],
        }
    if any(
        p.rstrip("/").endswith("/bh-viewer") or p.rstrip("/").endswith("/bh_viewer")
        for p in paths
    ):
        return {
            "access_type": "bh_viewer",
            "access_label": "BH Viewer",
            "states": [],
            "roles": [ROLE_BH_VIEWER],
        }
    states: list[str] = []
    roles: list[str] = []
    for p in paths:
        m = re.match(r"^/states/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/?$", p, re.I)
        if not m:
            continue
        states.append(m.group(1).upper())
        leaf = m.group(2).lower().replace("-", "_")
        roles.append(normalize_role(leaf) or leaf)
    # unique preserve order
    seen_s: set[str] = set()
    uniq_states = []
    for s in states:
        if s not in seen_s:
            seen_s.add(s)
            uniq_states.append(s)
    seen_r: set[str] = set()
    uniq_roles = []
    for r in roles:
        if r not in seen_r:
            seen_r.add(r)
            uniq_roles.append(r)

    if uniq_states:
        # e.g. "MH · contributor" or multi "MH · contributor, UP · reviewer"
        pairs = []
        if len(uniq_states) == len(uniq_roles):
            pairs = [f"{s} · {r}" for s, r in zip(uniq_states, uniq_roles)]
        elif uniq_roles:
            pairs = [f"{', '.join(uniq_states)} · {', '.join(uniq_roles)}"]
        else:
            pairs = list(uniq_states)
        return {
            "access_type": "state",
            "access_label": ", ".join(pairs),
            "states": uniq_states,
            "roles": uniq_roles,
        }

    # No product group → baseline dashboard / search-only access after SSO
    return {
        "access_type": "dashboard",
        "access_label": "Dashboard",
        "states": [],
        "roles": [],
    }


def list_realm_users(*, search: str = "", max_results: int = 100) -> dict[str, Any]:
    """List users in the SSO realm with groups (no delete)."""
    cfg = require_admin_config()
    token = _admin_token(cfg)
    admin = _admin_root(cfg)
    max_results = max(1, min(int(max_results or 100), 200))
    params: dict[str, Any] = {"max": max_results, "briefRepresentation": "false"}
    if (search or "").strip():
        params["search"] = search.strip()
    status, users = _req(
        "GET",
        f"{admin}/users?{urllib.parse.urlencode(params)}",
        token=token,
    )
    if not isinstance(users, list):
        raise HTTPException(502, "Unable to list Keycloak users")

    rows: list[dict[str, Any]] = []
    for u in users:
        if not isinstance(u, dict):
            continue
        uid = u.get("id")
        if not uid:
            continue
        try:
            _, groups = _req("GET", f"{admin}/users/{uid}/groups", token=token)
        except HTTPException:
            groups = []
        paths = [g.get("path") for g in (groups or []) if isinstance(g, dict) and g.get("path")]
        summary = _access_summary_from_groups(paths)
        first = (u.get("firstName") or "").strip()
        last = (u.get("lastName") or "").strip()
        name = f"{first} {last}".strip() or (u.get("username") or "")
        rows.append(
            {
                "user_id": uid,
                "username": u.get("username") or "",
                "email": u.get("email") or "",
                "name": name,
                "first_name": first,
                "last_name": last,
                "enabled": bool(u.get("enabled")),
                "email_verified": bool(u.get("emailVerified")),
                "groups": paths,
                "access_type": summary["access_type"],
                "access_label": summary["access_label"],
                "states": summary["states"],
                "roles": summary["roles"],
            }
        )

    # Prefer users with product groups first, then by email
    def sort_key(row: dict) -> tuple:
        rank = 0 if row.get("access_type") in ("super_admin", "bh_viewer", "state") else 1
        return (rank, (row.get("email") or row.get("username") or "").lower())

    rows.sort(key=sort_key)
    return {
        "total": len(rows),
        "users": rows,
        "realm": cfg.realm,
    }
