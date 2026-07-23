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

Admin credentials are read from env (KEYCLOAK_ADMIN / KEYCLOAK_ADMIN_PASSWORD).
Does not print passwords.
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

    print("bootstrap=ok")
    print(f"realm={args.realm}")
    print(f"organizations={','.join(TENANTS)}")
    print(f"groups={len(group_id_by_path)} (top + role groups)")
    print(f"users={','.join(EXAMPLE_USERS)}")
    if password_file:
        print(f"passwords_file={password_file} (mode 0600)")
    else:
        print("passwords_generated=yes (set KEYCLOAK_BOOTSTRAP_PASSWORD_FILE to capture them)")
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
