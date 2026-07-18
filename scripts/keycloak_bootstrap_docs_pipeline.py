#!/usr/bin/env python3
"""Ensure docs-pipeline Keycloak clients/roles/claims and a test curator exist.

Safe to re-run. Expects Keycloak already imported with realm amul-vistaar.
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
        if exc.code == 409:
            return exc.code, None
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("KEYCLOAK_BASE_URL", "http://127.0.0.1:8082/auth"))
    parser.add_argument("--realm", default="amul-vistaar")
    parser.add_argument("--admin-user", default=os.environ.get("KEYCLOAK_ADMIN", "admin"))
    parser.add_argument("--admin-password", default=os.environ.get("KEYCLOAK_ADMIN_PASSWORD", ""))
    parser.add_argument("--test-username", default="docs-test-curator")
    parser.add_argument("--test-password", default=os.environ.get("KEYCLOAK_TEST_USER_PASSWORD", ""))
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

    for role in ("master_admin", "admin", "content_curator", "viewer"):
        _req("POST", f"{admin}/roles", token=token, body={"name": role})

    def ensure_client(client_id: str, rep: dict) -> str:
        _, found = _req("GET", f"{admin}/clients?{urllib.parse.urlencode({'clientId': client_id})}", token=token)
        if found:
            return found[0]["id"]
        _req("POST", f"{admin}/clients", token=token, body=rep)
        _, found = _req("GET", f"{admin}/clients?{urllib.parse.urlencode({'clientId': client_id})}", token=token)
        return found[0]["id"]

    api_id = ensure_client(
        "docs-pipeline-api",
        {
            "clientId": "docs-pipeline-api",
            "enabled": True,
            "protocol": "openid-connect",
            "bearerOnly": True,
            "publicClient": False,
        },
    )
    ui_id = ensure_client(
        "docs-pipeline-ui",
        {
            "clientId": "docs-pipeline-ui",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": True,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False,
            "redirectUris": ["http://localhost:*/*", "https://search-ui.dev.amulai.in/*"],
            "webOrigins": ["http://localhost:*", "https://search-ui.dev.amulai.in"],
            "attributes": {"pkce.code.challenge.method": "S256"},
        },
    )
    test_id = ensure_client(
        "docs-pipeline-test-cli",
        {
            "clientId": "docs-pipeline-test-cli",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": True,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": True,
        },
    )
    _ = api_id

    mappers = [
        {
            "name": "docs-pipeline-api-audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.client.audience": "docs-pipeline-api",
                "access.token.claim": "true",
                "id.token.claim": "false",
            },
        },
        {
            "name": "instances-claim",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {
                "user.attribute": "instances",
                "claim.name": "instances",
                "jsonType.label": "String",
                "multivalued": "true",
                "access.token.claim": "true",
                "id.token.claim": "true",
                "userinfo.token.claim": "true",
            },
        },
        {
            "name": "envs-claim",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {
                "user.attribute": "envs",
                "claim.name": "envs",
                "jsonType.label": "String",
                "multivalued": "true",
                "access.token.claim": "true",
                "id.token.claim": "true",
                "userinfo.token.claim": "true",
            },
        },
    ]
    for client_uuid in (ui_id, test_id):
        _, existing = _req("GET", f"{admin}/clients/{client_uuid}/protocol-mappers/models", token=token)
        names = {item.get("name") for item in (existing or [])}
        for mapper in mappers:
            if mapper["name"] not in names:
                _req("POST", f"{admin}/clients/{client_uuid}/protocol-mappers/models", token=token, body=mapper)

    test_password = args.test_password or secrets.token_urlsafe(24)
    _, users = _req(
        "GET",
        f"{admin}/users?{urllib.parse.urlencode({'username': args.test_username, 'exact': 'true'})}",
        token=token,
    )
    user_body = {
        "username": args.test_username,
        "enabled": True,
        "attributes": {"instances": ["amul"], "envs": ["dev"]},
    }
    if users:
        uid = users[0]["id"]
        _req("PUT", f"{admin}/users/{uid}", token=token, body=user_body)
    else:
        _req("POST", f"{admin}/users", token=token, body=user_body)
        _, users = _req(
            "GET",
            f"{admin}/users?{urllib.parse.urlencode({'username': args.test_username, 'exact': 'true'})}",
            token=token,
        )
        uid = users[0]["id"]
    _req(
        "PUT",
        f"{admin}/users/{uid}/reset-password",
        token=token,
        body={"type": "password", "temporary": False, "value": test_password},
    )
    _, role = _req("GET", f"{admin}/roles/content_curator", token=token)
    _req("POST", f"{admin}/users/{uid}/role-mappings/realm", token=token, body=[role])

    # Smoke: password grant
    _, tok = _req(
        "POST",
        f"{base}/realms/{args.realm}/protocol/openid-connect/token",
        form={
            "grant_type": "password",
            "client_id": "docs-pipeline-test-cli",
            "username": args.test_username,
            "password": test_password,
        },
    )
    print("bootstrap=ok")
    print(f"realm={args.realm}")
    print(f"test_user={args.test_username}")
    print(f"token_acquired={bool(tok.get('access_token'))}")
    if not args.test_password:
        print("test_password_generated=yes (set KEYCLOAK_TEST_USER_PASSWORD to reuse)")
        # Write only if caller opts in via env path
        out = os.environ.get("KEYCLOAK_TEST_PASSWORD_FILE")
        if out:
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(test_password)
            os.chmod(out, 0o600)
            print(f"test_password_file={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
