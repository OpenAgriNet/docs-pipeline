#!/usr/bin/env python3
"""Idempotently configure Keycloak multi-state tenant groups + roles + mappers.

Creates:
  - Realm roles: super_admin, contributor, reviewer (+ legacy aliases)
  - Groups: /global/super-admin, /states/{STATE}/{contributor|reviewer}
  - Group → realm role mappings on leaf groups
  - Group Membership mapper (claim ``groups``, full path) on SPA clients

Safe to re-run. Does not print passwords.

Example:
  python scripts/keycloak_bootstrap_tenant_groups.py \\
    --base-url http://127.0.0.1:8082/auth \\
    --realm bharat-vistaar \\
    --admin-password "$KEYCLOAK_ADMIN_PASSWORD"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_STATES = [
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
    "MN",
    "ML",
    "MZ",
    "NL",
    "SK",
    "TR",
    "AN",
    "CH",
    "DN",
    "PY",
]

PRODUCT_ROLES = (
    ("super_admin", "Platform super admin — all states, full access"),
    ("contributor", "State contributor — upload / own-edit / own-delete / view"),
    ("reviewer", "State reviewer — edit/review/approve; no upload/delete"),
)

LEGACY_ALIASES = (
    ("master_admin", "super_admin"),
    ("content_curator", "contributor"),
    ("admin", "contributor"),
    ("viewer", "reviewer"),
)

SPA_CLIENT_IDS = (
    "bharat-vistaar",
    "docs-pipeline-ui",
    "docs-pipeline-test-cli",
)


def _req(method: str, url: str, *, token: str | None = None, body=None, form=None):
    headers = {}
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
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        detail = raw.decode("utf-8", errors="replace")
        if exc.code in (409, 404):
            return exc.code, None
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc


def _ensure_realm_role(admin: str, token: str, name: str, description: str = "") -> dict:
    code, role = _req("GET", f"{admin}/roles/{urllib.parse.quote(name)}", token=token)
    if code == 200 and role:
        return role
    _req(
        "POST",
        f"{admin}/roles",
        token=token,
        body={"name": name, "description": description},
    )
    _, role = _req("GET", f"{admin}/roles/{urllib.parse.quote(name)}", token=token)
    return role


def _find_group_by_path(admin: str, token: str, path: str) -> dict | None:
    # Keycloak 22+: /group-by-path/{path}
    code, group = _req(
        "GET",
        f"{admin}/group-by-path/{urllib.parse.quote(path.lstrip('/'), safe='/')}",
        token=token,
    )
    if code == 200 and group:
        return group
    return None


def _ensure_child_group(admin: str, token: str, parent_id: str | None, name: str) -> dict:
    """Create group under parent (or top-level when parent_id is None)."""
    if parent_id:
        _, children = _req("GET", f"{admin}/groups/{parent_id}/children", token=token)
        for child in children or []:
            if child.get("name") == name:
                return child
        _req("POST", f"{admin}/groups/{parent_id}/children", token=token, body={"name": name})
        _, children = _req("GET", f"{admin}/groups/{parent_id}/children", token=token)
        for child in children or []:
            if child.get("name") == name:
                return child
        raise RuntimeError(f"Failed to create child group {name} under {parent_id}")

    _, top = _req("GET", f"{admin}/groups?briefRepresentation=false", token=token)
    for g in top or []:
        if g.get("name") == name:
            return g
    _req("POST", f"{admin}/groups", token=token, body={"name": name})
    _, top = _req("GET", f"{admin}/groups?briefRepresentation=false", token=token)
    for g in top or []:
        if g.get("name") == name:
            return g
    raise RuntimeError(f"Failed to create top-level group {name}")


def _map_realm_role(admin: str, token: str, group_id: str, role: dict) -> None:
    _, existing = _req(
        "GET",
        f"{admin}/groups/{group_id}/role-mappings/realm",
        token=token,
    )
    names = {r.get("name") for r in (existing or [])}
    if role["name"] in names:
        return
    _req(
        "POST",
        f"{admin}/groups/{group_id}/role-mappings/realm",
        token=token,
        body=[{"id": role["id"], "name": role["name"]}],
    )


def _ensure_groups_mapper(admin: str, token: str, client_uuid: str) -> None:
    _, existing = _req(
        "GET",
        f"{admin}/clients/{client_uuid}/protocol-mappers/models",
        token=token,
    )
    names = {m.get("name") for m in (existing or [])}
    if "groups" in names:
        # Ensure full.path is true if present
        for m in existing or []:
            if m.get("name") == "groups" and m.get("protocolMapper") == "oidc-group-membership-mapper":
                cfg = m.get("config") or {}
                if str(cfg.get("full.path", "")).lower() != "true":
                    m["config"] = {
                        **cfg,
                        "full.path": "true",
                        "claim.name": "groups",
                        "access.token.claim": "true",
                        "id.token.claim": "true",
                        "userinfo.token.claim": "true",
                    }
                    _req(
                        "PUT",
                        f"{admin}/clients/{client_uuid}/protocol-mappers/models/{m['id']}",
                        token=token,
                        body=m,
                    )
                return
        return

    _req(
        "POST",
        f"{admin}/clients/{client_uuid}/protocol-mappers/models",
        token=token,
        body={
            "name": "groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "consentRequired": False,
            "config": {
                "full.path": "true",
                "id.token.claim": "true",
                "access.token.claim": "true",
                "claim.name": "groups",
                "userinfo.token.claim": "true",
            },
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("KEYCLOAK_BASE_URL", "http://127.0.0.1:8082/auth"),
    )
    parser.add_argument("--realm", default=os.environ.get("KEYCLOAK_REALM", "bharat-vistaar"))
    parser.add_argument("--admin-user", default=os.environ.get("KEYCLOAK_ADMIN", "admin"))
    parser.add_argument("--admin-password", default=os.environ.get("KEYCLOAK_ADMIN_PASSWORD", ""))
    parser.add_argument(
        "--states",
        default=",".join(DEFAULT_STATES),
        help="Comma-separated state codes (default: full India list)",
    )
    args = parser.parse_args()
    if not args.admin_password:
        print("KEYCLOAK_ADMIN_PASSWORD / --admin-password required", file=sys.stderr)
        return 2

    base = args.base_url.rstrip("/")
    _, token_body = _req(
        "POST",
        f"{base}/realms/master/protocol/openid-connect/token",
        form={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": args.admin_user,
            "password": args.admin_password,
        },
    )
    token = token_body["access_token"]
    admin = f"{base}/admin/realms/{args.realm}"

    # --- Roles ---
    role_by_name: dict[str, dict] = {}
    for name, desc in PRODUCT_ROLES:
        role_by_name[name] = _ensure_realm_role(admin, token, name, desc)
        print(f"role=ok name={name}")

    for alias, target in LEGACY_ALIASES:
        role = _ensure_realm_role(admin, token, alias, f"Legacy alias → {target}")
        # Best-effort composite link
        try:
            _req(
                "POST",
                f"{admin}/roles/{urllib.parse.quote(alias)}/composites",
                token=token,
                body=[{"id": role_by_name[target]["id"], "name": target}],
            )
        except RuntimeError:
            pass
        print(f"role_alias=ok name={alias} -> {target}")

    # --- Groups ---
    global_g = _ensure_child_group(admin, token, None, "global")
    super_g = _ensure_child_group(admin, token, global_g["id"], "super-admin")
    _map_realm_role(admin, token, super_g["id"], role_by_name["super_admin"])
    print("group=ok path=/global/super-admin")

    states_g = _ensure_child_group(admin, token, None, "states")
    states = [s.strip().upper() for s in args.states.split(",") if s.strip()]
    for state in states:
        state_g = _ensure_child_group(admin, token, states_g["id"], state)
        for leaf, role_name in (("contributor", "contributor"), ("reviewer", "reviewer")):
            leaf_g = _ensure_child_group(admin, token, state_g["id"], leaf)
            _map_realm_role(admin, token, leaf_g["id"], role_by_name[role_name])
        print(f"group=ok path=/states/{state}/{{contributor,reviewer}}")

    # --- Client mappers ---
    for client_id in SPA_CLIENT_IDS:
        _, found = _req(
            "GET",
            f"{admin}/clients?{urllib.parse.urlencode({'clientId': client_id})}",
            token=token,
        )
        if not found:
            print(f"client=skip missing={client_id}")
            continue
        _ensure_groups_mapper(admin, token, found[0]["id"])
        print(f"mapper=ok client={client_id} name=groups")

    print("bootstrap_tenant_groups=ok")
    print(f"realm={args.realm}")
    print(f"states={len(states)}")
    print("next=assign users to groups in Keycloak Admin UI; re-login to refresh JWT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
