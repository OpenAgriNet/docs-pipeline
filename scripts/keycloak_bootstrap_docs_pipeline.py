#!/usr/bin/env python3
"""Bootstrap the docs-pipeline Keycloak 26 realm with example tenants + users.

Clean-redeploy companion to keycloak/import/docs-pipeline-realm.json. The realm
export ships the three clients, Organizations (enabled), the master_admin realm
role, and the per-tenant group/role model — but NO users. This script fills in
the example tenants and users so a fresh 26 deployment is immediately testable.

What it does (all idempotent, safe to re-run):
  - Ensures the master_admin realm role exists.
  - Ensures two example tenants exist as Keycloak *Organizations*: tenant-a, tenant-b.
  - Ensures the per-tenant groups exist: /<tenant>/<role> for
    role in {admin, content_curator, viewer}.
  - Creates example users, each joined to a role-group (and, where applicable,
    added as a member of the matching Organization):
      demo-admin      -> /tenant-a/admin        (org tenant-a)
      demo-curator    -> /tenant-a/content_curator (org tenant-a)
      demo-viewer     -> /tenant-b/viewer        (org tenant-b)
      platform-admin  -> realm role master_admin (unrestricted; no tenant)
  - Sets the back-compat "instances"/"envs" user attributes to match membership.
  - Sets a generated password per user (temporary=false). Passwords are NOT printed;
    set KEYCLOAK_BOOTSTRAP_PASSWORD_FILE to capture them (mode 0600).
  - Ensures the confidential service-account client `docs-pipeline-admin` (shipped by
    the realm export) has the realm-management `realm-admin` role on its service
    account, so the backend's client-credentials token can create/manage
    Organizations, users, groups, and group memberships via the Admin API.
  - Prints that client's secret so an operator can paste it into the backend .env as
    KEYCLOAK_ADMIN_CLIENT_SECRET. The realm export ships NO secret (Keycloak generates
    one on import); this script AUTO-rotates a missing/placeholder secret before
    printing, and --regenerate-admin-secret forces a rotation. This is a CLIENT secret,
    not a user password.

Admin credentials are read from env (KEYCLOAK_ADMIN / KEYCLOAK_ADMIN_PASSWORD).
Does not print user passwords (it does print the docs-pipeline-admin client secret,
which the backend needs — treat stdout as sensitive).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request


ROLES = ("admin", "content_curator", "viewer")
TENANTS = ("tenant-a", "tenant-b")

# Confidential service-account client the backend uses to call the KC Admin API.
ADMIN_CLIENT_ID = "docs-pipeline-admin"
# Legacy placeholder that used to ship in the realm export. If a deployment still
# carries it, we AUTO-rotate to a real secret so the well-known value never works.
# (Kept in sync with pipeline.keycloak_admin.PLACEHOLDER_ADMIN_SECRET.)
PLACEHOLDER_ADMIN_SECRET = "CHANGE_ME_ADMIN_SECRET"
# realm-management client-role granted to that service account. realm-admin is the
# realm-management composite that transitively includes manage-users, manage-realm,
# view-users, query-users and query-groups AND all organization-management perms,
# so it reliably covers creating/managing orgs, users, groups and memberships in
# KC 26 without pinning the exact minimal subset (org endpoints are gated behind
# manage-realm/view-realm, which realm-admin includes).
ADMIN_SERVICE_ACCOUNT_ROLE = "realm-admin"

# username -> (group_path | None, realm_roles, instances, envs)
EXAMPLE_USERS = {
    "demo-admin": ("/tenant-a/admin", [], ["tenant-a"], ["dev"]),
    "demo-curator": ("/tenant-a/content_curator", [], ["tenant-a"], ["dev"]),
    "demo-viewer": ("/tenant-b/viewer", [], ["tenant-b"], ["dev"]),
    "platform-admin": (None, ["master_admin"], [], ["dev", "prod"]),
}


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
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        detail = raw.decode("utf-8", errors="replace")
        # 409 = already exists; treat as idempotent no-op.
        if exc.code == 409:
            return exc.code, None
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("KEYCLOAK_BASE_URL", "http://127.0.0.1:8082/auth"))
    parser.add_argument("--realm", default="docs-pipeline")
    parser.add_argument("--admin-user", default=os.environ.get("KEYCLOAK_ADMIN", "admin"))
    parser.add_argument("--admin-password", default=os.environ.get("KEYCLOAK_ADMIN_PASSWORD", ""))
    parser.add_argument(
        "--print-admin-secret",
        action="store_true",
        help=f"Print the {ADMIN_CLIENT_ID} client secret (also printed by default at the end of a run).",
    )
    parser.add_argument(
        "--regenerate-admin-secret",
        action="store_true",
        help=f"Regenerate the {ADMIN_CLIENT_ID} client secret before printing it (rotates the placeholder).",
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

    # 1) Realm role: master_admin (the realm export also ships it; ensure for re-runs on older realms).
    _req("POST", f"{admin}/roles", token=token, body={"name": "master_admin"})

    # 2) Organizations (KC 26): one per example tenant. Idempotent by name.
    _, existing_orgs = _req("GET", f"{admin}/organizations", token=token)
    orgs_by_name = {org["name"]: org["id"] for org in (existing_orgs or [])}
    for tenant in TENANTS:
        if tenant not in orgs_by_name:
            _req(
                "POST",
                f"{admin}/organizations",
                token=token,
                body={
                    "name": tenant,
                    "alias": tenant,
                    "enabled": True,
                    "domains": [{"name": f"{tenant}.example.com", "verified": False}],
                    "attributes": {"instance": [tenant]},
                },
            )
    _, existing_orgs = _req("GET", f"{admin}/organizations", token=token)
    orgs_by_name = {org["name"]: org["id"] for org in (existing_orgs or [])}

    # 3) Groups: /<tenant> with child role-groups /<tenant>/<role>. Idempotent.
    def ensure_top_group(name: str) -> str:
        _, groups = _req("GET", f"{admin}/groups?{urllib.parse.urlencode({'search': name})}", token=token)
        for group in groups or []:
            if group["name"] == name:
                return group["id"]
        _req("POST", f"{admin}/groups", token=token, body={"name": name})
        _, groups = _req("GET", f"{admin}/groups?{urllib.parse.urlencode({'search': name})}", token=token)
        for group in groups or []:
            if group["name"] == name:
                return group["id"]
        raise RuntimeError(f"could not create/find group {name}")

    def ensure_child_group(parent_id: str, name: str) -> str:
        _, children = _req("GET", f"{admin}/groups/{parent_id}/children", token=token)
        for child in children or []:
            if child["name"] == name:
                return child["id"]
        _req("POST", f"{admin}/groups/{parent_id}/children", token=token, body={"name": name})
        _, children = _req("GET", f"{admin}/groups/{parent_id}/children", token=token)
        for child in children or []:
            if child["name"] == name:
                return child["id"]
        raise RuntimeError(f"could not create/find child group {name}")

    group_id_by_path: dict[str, str] = {}
    for tenant in TENANTS:
        top_id = ensure_top_group(tenant)
        group_id_by_path[f"/{tenant}"] = top_id
        for role in ROLES:
            child_id = ensure_child_group(top_id, role)
            group_id_by_path[f"/{tenant}/{role}"] = child_id

    # 4) Users + membership.
    password_file = os.environ.get("KEYCLOAK_BOOTSTRAP_PASSWORD_FILE")
    generated: dict[str, str] = {}

    def ensure_user(username: str, instances: list[str], envs: list[str]) -> str:
        _, users = _req(
            "GET",
            f"{admin}/users?{urllib.parse.urlencode({'username': username, 'exact': 'true'})}",
            token=token,
        )
        # firstName/lastName are required by Keycloak 26's declarative User Profile —
        # without them the account is "not fully set up" and password-grant login fails.
        _name = username.replace("-", " ").replace("_", " ").title()
        rep = {
            "username": username,
            "email": f"{username}@example.com",
            "emailVerified": True,
            "enabled": True,
            "firstName": _name.split(" ")[0] if _name else username,
            "lastName": _name.split(" ", 1)[1] if " " in _name else "User",
            "attributes": {"instances": instances, "envs": envs},
        }
        if users:
            uid = users[0]["id"]
            _req("PUT", f"{admin}/users/{uid}", token=token, body=rep)
            return uid
        _req("POST", f"{admin}/users", token=token, body=rep)
        _, users = _req(
            "GET",
            f"{admin}/users?{urllib.parse.urlencode({'username': username, 'exact': 'true'})}",
            token=token,
        )
        return users[0]["id"]

    for username, (group_path, realm_roles, instances, envs) in EXAMPLE_USERS.items():
        uid = ensure_user(username, instances, envs)

        # Password (generated, non-temporary). Not printed.
        password = secrets.token_urlsafe(24)
        _req(
            "PUT",
            f"{admin}/users/{uid}/reset-password",
            token=token,
            body={"type": "password", "temporary": False, "value": password},
        )
        generated[username] = password

        # Group membership (join is idempotent; KC returns 204/no-op if already a member).
        if group_path:
            gid = group_id_by_path[group_path]
            _req("PUT", f"{admin}/users/{uid}/groups/{gid}", token=token, body={})

        # Realm-role assignment (e.g. master_admin).
        for role_name in realm_roles:
            _, role = _req("GET", f"{admin}/roles/{urllib.parse.quote(role_name)}", token=token)
            if role:
                _req("POST", f"{admin}/users/{uid}/role-mappings/realm", token=token, body=[role])

        # Organization membership: derive tenant from the top-level group segment.
        if group_path:
            tenant = group_path.strip("/").split("/")[0]
            org_id = orgs_by_name.get(tenant)
            if org_id:
                # KC 26: POST the user id (as a JSON string) to the org members endpoint.
                _req("POST", f"{admin}/organizations/{org_id}/members", token=token, body=uid)

    if password_file:
        with open(password_file, "w", encoding="utf-8") as handle:
            for username, password in generated.items():
                handle.write(f"{username}\t{password}\n")
        os.chmod(password_file, 0o600)

    # 5) Admin API service-account client: ensure its service account holds the
    #    realm-admin role, then read (or regenerate) and print its secret so an
    #    operator can set KEYCLOAK_ADMIN_CLIENT_SECRET in the backend .env.
    admin_secret = None
    admin_secret_rotated = False
    _, admin_clients = _req(
        "GET",
        f"{admin}/clients?{urllib.parse.urlencode({'clientId': ADMIN_CLIENT_ID})}",
        token=token,
    )
    admin_client = (admin_clients or [None])[0]
    if admin_client:
        admin_uuid = admin_client["id"]

        # Grant realm-management realm-admin on the service account (idempotent).
        _, rm_clients = _req(
            "GET",
            f"{admin}/clients?{urllib.parse.urlencode({'clientId': 'realm-management'})}",
            token=token,
        )
        rm_client = (rm_clients or [None])[0]
        _, sa_user = _req("GET", f"{admin}/clients/{admin_uuid}/service-account-user", token=token)
        if rm_client and sa_user:
            rm_uuid = rm_client["id"]
            sa_uid = sa_user["id"]
            _, role = _req(
                "GET",
                f"{admin}/clients/{rm_uuid}/roles/{urllib.parse.quote(ADMIN_SERVICE_ACCOUNT_ROLE)}",
                token=token,
            )
            if role:
                # POST to client role-mappings is a no-op if the role is already assigned.
                _req(
                    "POST",
                    f"{admin}/users/{sa_uid}/role-mappings/clients/{rm_uuid}",
                    token=token,
                    body=[role],
                )

        # Read the current client secret; AUTO-rotate it when missing or still the
        # well-known placeholder (a fresh KC 26 import generates a real one, but an
        # older deployment may still carry the placeholder). --regenerate-admin-secret
        # forces a rotation regardless.
        _, secret_body = _req("GET", f"{admin}/clients/{admin_uuid}/client-secret", token=token)
        current_secret = (secret_body or {}).get("value")
        needs_rotation = (
            args.regenerate_admin_secret
            or not current_secret
            or current_secret == PLACEHOLDER_ADMIN_SECRET
        )
        if needs_rotation:
            _, secret_body = _req("POST", f"{admin}/clients/{admin_uuid}/client-secret", token=token)
            admin_secret_rotated = True
        admin_secret = (secret_body or {}).get("value")

    print("bootstrap=ok")
    print(f"realm={args.realm}")
    print(f"organizations={','.join(TENANTS)}")
    print(f"groups={len(group_id_by_path)} (top + role groups)")
    print(f"users={','.join(EXAMPLE_USERS)}")
    if password_file:
        print(f"passwords_file={password_file} (mode 0600)")
    else:
        print("passwords_generated=yes (set KEYCLOAK_BOOTSTRAP_PASSWORD_FILE to capture them)")

    # Admin service-account client summary + secret for the backend .env.
    if admin_client:
        print(f"admin_client={ADMIN_CLIENT_ID} (service account -> realm-management:{ADMIN_SERVICE_ACCOUNT_ROLE})")
        if admin_secret:
            print(f"admin_secret_rotated={'yes' if admin_secret_rotated else 'no'}")
            print("# Copy this into the backend .env as KEYCLOAK_ADMIN_CLIENT_SECRET (sensitive):")
            print(f"KEYCLOAK_ADMIN_CLIENT_SECRET={admin_secret}")
        else:
            print(f"admin_secret=unavailable (query via KC admin console: Clients > {ADMIN_CLIENT_ID} > Credentials)")
    else:
        print(f"admin_client={ADMIN_CLIENT_ID}=MISSING (is the realm export imported?)")
    print("")
    print("# Verify the groups claim (needs a captured password):")
    print("#   curl -s -X POST \\")
    print(f"#     {base}/realms/{args.realm}/protocol/openid-connect/token \\")
    print("#     -d grant_type=password -d client_id=docs-pipeline-test-cli \\")
    print("#     -d username=demo-curator -d password=<generated> | \\")
    print("#     python3 -c 'import sys,json,base64; t=json.load(sys.stdin)[\"access_token\"];"
          " p=t.split(\".\")[1]; p+=\"=\"*(-len(p)%4);"
          " print(json.dumps(json.loads(base64.urlsafe_b64decode(p)), indent=2))'")
    print("# Expect: \"groups\": [\"/tenant-a/content_curator\"], \"instances\": [\"tenant-a\"]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
