#!/usr/bin/env python3
"""Remove pre-six-role Keycloak roles/groups and the direct role assignments.

Run this LAST, after ``keycloak_bootstrap_tenant_groups.py`` has created the
new tree and every user has been moved into the right group. Ordering matters:
deleting a role before its holders are in a group locks those people out.

Three cleanups, each independently toggleable:

1. ``--drop-direct-roles`` — remove user→realm-role assignments so group
   membership becomes the only source of access.
2. ``--delete-roles``      — delete the retired realm roles.
3. ``--delete-groups``     — delete empty retired leaf groups.

Refuses to strip a direct role from anyone who would be left with no product
group — that is the lockout case. Those users are reported and skipped; add
them to a group first, then re-run.

DRY RUN BY DEFAULT. Nothing is deleted unless you pass --apply.

Example:
  python scripts/keycloak_purge_legacy_roles.py --drop-direct-roles --delete-roles
  python scripts/keycloak_purge_legacy_roles.py --drop-direct-roles --delete-roles --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# Realm roles retired by the six-role model. ``super-admin`` is the
# pre-consolidation spelling that folds into ``super_admin``.
RETIRED_ROLES = (
    "super-admin",
    "contributor",
    "reviewer",
    "viewer",
    "content_curator",
    "master_admin",
    "admin",
    "admin-bharat",
    "admin-bihar",
)

# Leaf group names no longer part of the model. ``contributor`` is NOT here —
# it is a live leaf again, now meaning the weakest upload role.
RETIRED_LEAVES = ("reviewer",)

# Roles that must only ever reach a token through a group.
GROUPED_ROLES = (
    "super_admin",
    "bh_viewer",
    "state_admin",
    "state_approver",
    "state_contributor",
    "state_view",
)

# Roles that still grant real access, so stripping one from a user with no
# product group locks them out. This is NOT the same as GROUPED_ROLES:
# ``super-admin`` is retired but stays mapped in permissions.py until its last
# holder is in /global/super-admin, and it is the single most dangerous role to
# strip blind. Roles absent here (admin-bharat, admin-bihar, …) map to nothing
# in permissions.py, so removing them cannot change what anyone can do.
ACCESS_GRANTING_ROLES = frozenset(GROUPED_ROLES) | {"super-admin", "superadmin"}


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
        if exc.code in (404, 409):
            return exc.code, None
        raise RuntimeError(f"{method} {url} -> {exc.code}: {exc.read().decode()[:300]}") from exc


def _label(user: dict) -> str:
    return user.get("email") or user.get("username") or user.get("id", "?")


def _all_groups(admin: str, token: str) -> list[dict]:
    """Flatten the group tree into a list of {id, path} dicts."""
    _, top = _req("GET", f"{admin}/groups?max=500&briefRepresentation=false", token=token)
    flat: list[dict] = []

    def walk(nodes):
        for node in nodes or []:
            flat.append(node)
            walk(node.get("subGroups"))

    walk(top)
    return flat


def _is_product_group(path: str) -> bool:
    """True when a group path grants a product role in the six-role model."""
    clean = (path or "").rstrip("/")
    if clean in ("/global/super-admin", "/global/bh-viewer"):
        return True
    parts = [p for p in clean.split("/") if p]
    return len(parts) == 3 and parts[0] == "states"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=os.environ.get("KEYCLOAK_ADMIN_BASE_URL", "")
    )
    parser.add_argument("--realm", default=os.environ.get("KEYCLOAK_ADMIN_REALM", "bharat-vistaar"))
    parser.add_argument(
        "--admin-user", default=os.environ.get("KEYCLOAK_ADMIN_USERNAME", "admin")
    )
    parser.add_argument("--admin-password", default=os.environ.get("KEYCLOAK_ADMIN_PASSWORD", ""))
    parser.add_argument(
        "--token-realm", default=os.environ.get("KEYCLOAK_ADMIN_TOKEN_REALM", "master")
    )
    parser.add_argument(
        "--skip-user",
        action="append",
        default=[],
        metavar="EMAIL",
        help=(
            "Leave this user's direct roles alone. Use for anyone whose strip "
            "would be a demotion rather than a no-op (they are in a group, so "
            "the lockout guard will not catch it). Repeatable."
        ),
    )
    parser.add_argument("--drop-direct-roles", action="store_true")
    parser.add_argument("--delete-roles", action="store_true")
    parser.add_argument("--delete-groups", action="store_true")
    parser.add_argument(
        "--apply", action="store_true", help="Actually make changes (default: dry run)"
    )
    args = parser.parse_args()

    if not args.base_url or not args.admin_password:
        print("--base-url and --admin-password (or env equivalents) required", file=sys.stderr)
        return 2
    if not (args.drop_direct_roles or args.delete_roles or args.delete_groups):
        print("Nothing to do — pass at least one of --drop-direct-roles / "
              "--delete-roles / --delete-groups", file=sys.stderr)
        return 2

    mode = "APPLY" if args.apply else "DRY RUN"
    skip_users = set(args.skip_user)
    base = args.base_url.rstrip("/")
    _, token_body = _req(
        "POST",
        f"{base}/realms/{args.token_realm}/protocol/openid-connect/token",
        form={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": args.admin_user,
            "password": args.admin_password,
        },
    )
    token = token_body["access_token"]
    admin = f"{base}/admin/realms/{args.realm}"
    print(f"=== {mode} — realm={args.realm} ===\n")

    # Map every user that holds a product group, so we never orphan anyone.
    grouped_user_ids: set[str] = set()
    for group in _all_groups(admin, token):
        if not _is_product_group(group.get("path", "")):
            continue
        _, members = _req("GET", f"{admin}/groups/{group['id']}/members?max=500", token=token)
        for member in members or []:
            grouped_user_ids.add(member["id"])

    blocked: list[str] = []

    if args.drop_direct_roles:
        print("--- direct realm-role assignments ---")
        for role_name in GROUPED_ROLES + RETIRED_ROLES:
            status, role = _req(
                "GET", f"{admin}/roles/{urllib.parse.quote(role_name)}", token=token
            )
            if status != 200 or not role:
                continue
            _, holders = _req(
                "GET",
                f"{admin}/roles/{urllib.parse.quote(role_name)}/users?max=500",
                token=token,
            )
            for holder in holders or []:
                who = _label(holder)
                if who in skip_users:
                    print(f"  HOLD  {role_name:18s} {who}  -> --skip-user")
                    continue
                # Only roles that still grant access can lock someone out.
                if role_name in ACCESS_GRANTING_ROLES and holder["id"] not in grouped_user_ids:
                    blocked.append(f"{who} (holds {role_name}, in no product group)")
                    print(f"  SKIP  {role_name:18s} {who}  -> would lose all access")
                    continue
                print(f"  strip {role_name:18s} {who}")
                if args.apply:
                    _req(
                        "DELETE",
                        f"{admin}/users/{holder['id']}/role-mappings/realm",
                        token=token,
                        body=[{"id": role["id"], "name": role["name"]}],
                    )
        print()

    if args.delete_groups:
        print("--- retired leaf groups ---")
        for group in _all_groups(admin, token):
            path = group.get("path", "")
            if path.rstrip("/").rsplit("/", 1)[-1] not in RETIRED_LEAVES:
                continue
            _, members = _req(
                "GET", f"{admin}/groups/{group['id']}/members?max=10", token=token
            )
            if members:
                print(f"  SKIP  {path}  -> still has {len(members)} member(s)")
                continue
            print(f"  delete {path}")
            if args.apply:
                _req("DELETE", f"{admin}/groups/{group['id']}", token=token)
        print()

    if args.delete_roles:
        print("--- retired realm roles ---")
        for role_name in RETIRED_ROLES:
            status, role = _req(
                "GET", f"{admin}/roles/{urllib.parse.quote(role_name)}", token=token
            )
            if status != 200 or not role:
                continue
            _, holders = _req(
                "GET",
                f"{admin}/roles/{urllib.parse.quote(role_name)}/users?max=500",
                token=token,
            )
            if holders:
                print(
                    f"  SKIP  {role_name}  -> still assigned to "
                    f"{len(holders)} user(s): {', '.join(_label(h) for h in holders[:5])}"
                )
                continue
            print(f"  delete role {role_name}")
            if args.apply:
                _req("DELETE", f"{admin}/roles/{urllib.parse.quote(role_name)}", token=token)
        print()

    if blocked:
        print("!! These users would have been locked out and were SKIPPED:")
        for entry in blocked:
            print(f"   - {entry}")
        print("   Add them to the right Keycloak group, then re-run.\n")

    print(f"purge={mode.lower().replace(' ', '_')} realm={args.realm}")
    if not args.apply:
        print("No changes were made. Re-run with --apply once the plan above looks right.")
    else:
        print("next=affected users must sign out and back in to refresh their JWT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
