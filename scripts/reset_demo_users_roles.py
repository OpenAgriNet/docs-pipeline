#!/usr/bin/env python3
"""Reset the three Akshat demo users to a clean 1:1 role mapping.

Removes old product groups + realm roles, then assigns:

  akshat.rana@kenpath.io     → BV Super Admin  (/global/super-admin)
  akshatrana262@gmail.com    → State Admin     (/states/{STATE}/admin)
  akshatrana033@gmail.com    → State View      (/states/{STATE}/view)

Default state: MH (override with --state).
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_env_path = ROOT / ".env"
if _env_path.is_file():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip("'").strip('"'))

from fastapi import HTTPException  # noqa: E402

from pipeline.auth.groups import (  # noqa: E402
    ROLE_STATE_ADMIN,
    ROLE_STATE_VIEW,
    ROLE_SUPER_ADMIN,
)
from pipeline.auth.keycloak_admin import (  # noqa: E402
    _admin_root,
    _admin_token,
    _req,
    ensure_access_group,
    load_keycloak_admin_config,
    provision_user,
)

# Product / legacy roles to strip from users (keep default-roles-* alone).
PRODUCT_ROLE_NAMES = frozenset(
    {
        ROLE_SUPER_ADMIN,
        ROLE_STATE_ADMIN,
        ROLE_STATE_VIEW,
        "superadmin",
        "super-admin",
        "master_admin",
        "master-admin",
        "admin",
        "contributor",
        "content_curator",
        "curator",
        "operator",
        "reviewer",
        "viewer",
        "view",
        "reader",
        "state-admin",
        "state-view",
    }
)

ASSIGNMENTS = [
    {
        "email": "akshat.rana@kenpath.io",
        "first_name": "Akshat",
        "last_name": "Rana",
        "access_type": "super_admin",
        "state": None,
        "role": None,
        "label": "BV Super Admin",
    },
    {
        "email": "akshatrana262@gmail.com",
        "first_name": "Akshat",
        "last_name": "Rana",
        "access_type": "state",
        "state": None,  # filled from --state
        "role": ROLE_STATE_ADMIN,
        "label": "State Admin",
    },
    {
        "email": "akshatrana033@gmail.com",
        "first_name": "Akshat",
        "last_name": "Rana",
        "access_type": "state",
        "state": None,
        "role": ROLE_STATE_VIEW,
        "label": "State View",
    },
]


def _find_user(admin: str, token: str, email: str) -> dict | None:
    status, found = _req(
        "GET",
        f"{admin}/users?{urllib.parse.urlencode({'email': email, 'exact': 'true'})}",
        token=token,
    )
    if isinstance(found, list) and found:
        return found[0]
    return None


def _is_product_group(path: str | None) -> bool:
    p = (path or "").strip()
    if not p:
        return False
    if p.startswith("/global"):
        return True
    if p.startswith("/states"):
        return True
    # Flat names some installs use
    if "super-admin" in p or p in ("contributor", "reviewer"):
        return True
    return False


def clear_product_access(admin: str, token: str, user_id: str) -> dict:
    """Remove product groups + product realm roles from a user."""
    removed_groups: list[str] = []
    removed_roles: list[str] = []

    _, groups = _req("GET", f"{admin}/users/{user_id}/groups", token=token)
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        path = g.get("path") or g.get("name") or ""
        gid = g.get("id")
        if not gid or not _is_product_group(path):
            continue
        try:
            _req("DELETE", f"{admin}/users/{user_id}/groups/{gid}", token=token)
            removed_groups.append(path)
        except HTTPException as exc:
            print(f"  warn: could not leave group {path}: {exc.detail}")

    _, mapped = _req("GET", f"{admin}/users/{user_id}/role-mappings/realm", token=token)
    to_delete = []
    for r in mapped or []:
        if not isinstance(r, dict):
            continue
        name = (r.get("name") or "").strip()
        if name in PRODUCT_ROLE_NAMES or name.lower() in PRODUCT_ROLE_NAMES:
            to_delete.append({"id": r["id"], "name": r["name"]})
            removed_roles.append(name)
    if to_delete:
        try:
            _req(
                "DELETE",
                f"{admin}/users/{user_id}/role-mappings/realm",
                token=token,
                body=to_delete,
            )
        except HTTPException as exc:
            # Some Keycloak versions want DELETE with body differently; try one-by-one.
            print(f"  warn: bulk role delete failed ({exc.detail}); trying per-role")
            for role in to_delete:
                try:
                    _req(
                        "DELETE",
                        f"{admin}/users/{user_id}/role-mappings/realm",
                        token=token,
                        body=[role],
                    )
                except HTTPException as exc2:
                    print(f"  warn: could not remove role {role.get('name')}: {exc2.detail}")

    return {"removed_groups": removed_groups, "removed_roles": removed_roles}


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset demo users to clean role mapping")
    parser.add_argument(
        "--state",
        default="MH",
        help="State code for state_admin / state_view users (default MH)",
    )
    args = parser.parse_args()
    state = args.state.strip().upper()

    cfg = load_keycloak_admin_config()
    if not cfg.configured:
        print("ERROR: Keycloak admin not configured", file=sys.stderr)
        return 1

    print(f"Keycloak → {cfg.base_url} realm={cfg.realm}")
    print(f"State for admin/view users: {state}")
    print()

    # Ensure target groups exist first
    ensure_access_group(cfg, access_type="super_admin", state=None, role=None)
    ensure_access_group(cfg, access_type="state", state=state, role=ROLE_STATE_ADMIN)
    ensure_access_group(cfg, access_type="state", state=state, role=ROLE_STATE_VIEW)

    token = _admin_token(cfg)
    admin = _admin_root(cfg)

    results = []
    for spec in ASSIGNMENTS:
        email = spec["email"]
        print(f"=== {email} → {spec['label']} ===")
        user = _find_user(admin, token, email)
        if not user:
            print("  user not found — will create on provision")
        else:
            cleared = clear_product_access(admin, token, user["id"])
            print(f"  removed groups: {cleared['removed_groups'] or '(none)'}")
            print(f"  removed roles:  {cleared['removed_roles'] or '(none)'}")

        access_type = spec["access_type"]
        role = spec["role"]
        st = state if access_type == "state" else None
        result = provision_user(
            email=email,
            first_name=spec["first_name"],
            last_name=spec["last_name"],
            access_type=access_type,
            state=st,
            role=role,
            enabled=True,
        )
        print(f"  groups now: {result.get('groups')}")
        print(f"  roles now:  {result.get('roles')}")
        print()
        results.append(
            {
                "email": email,
                "label": spec["label"],
                "access_type": result.get("access_type"),
                "state": result.get("state"),
                "role": result.get("role"),
                "group_path": result.get("group_path"),
                "groups": result.get("groups"),
                "roles": result.get("roles"),
            }
        )

    print("========== FINAL MAPPING ==========")
    for r in results:
        if r["access_type"] == "super_admin":
            print(f"  {r['email']}")
            print(f"    Role:  BV Super Admin")
            print(f"    Group: /global/super-admin")
        else:
            print(f"  {r['email']}")
            print(f"    Role:  {r['label']} ({r['role']})")
            print(f"    Group: {r['group_path']}")
            print(f"    State: {r['state']}")
        print(f"    Groups claim: {r['groups']}")
        print(f"    Realm roles:  {[x for x in (r['roles'] or []) if not str(x).startswith('default-roles-')]}")
        print()
    print("Sign out and SSO again so new groups appear in the JWT.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
