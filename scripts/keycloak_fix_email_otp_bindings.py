#!/usr/bin/env python3
"""Repair the ``bharat-vistaar`` client's authentication-flow bindings.

Two things are wrong on the realm as shipped:

1. ``email-otp-direct-grant`` is bound to the client's **browser** slot. A
   direct-grant flow cannot serve a browser authorize request, so
   ``/protocol/openid-connect/auth?client_id=bharat-vistaar`` answers
   ``401 {"error":"invalid_request","error_description":"Missing parameter: username"}``
   and SSO on that client is dead.
2. Nothing is bound to the **direct_grant** slot, so the realm's built-in
   password flow runs instead. ``grant_type=password`` + ``otp=<code>`` is
   therefore checked as a *password*, and every real OTP comes back
   ``invalid_grant / Invalid user credentials``.

Fix: clear the browser override (fall back to the realm browser flow, which is
what ``docs-pipeline-ui`` already uses successfully) and bind
``email-otp-direct-grant`` to ``direct_grant``.

Idempotent — re-running when already correct changes nothing.

    python scripts/keycloak_fix_email_otp_bindings.py [--dry-run]

Reads KEYCLOAK_ADMIN_* from the repo-root .env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

OTP_FLOW_ALIAS = "email-otp-direct-grant"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.is_file():
        load_dotenv(env, override=False)


def _request(method: str, url: str, token: str | None = None, body: dict | None = None):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw.decode()) if raw else None


def _admin_token(base: str, token_realm: str, user: str, password: str) -> str:
    form = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": user,
            "password": password,
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/realms/{token_realm}/protocol/openid-connect/token",
        data=form,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["access_token"]


def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("KEYCLOAK_ADMIN_BASE_URL"))
    parser.add_argument("--realm", default=os.environ.get("KEYCLOAK_ADMIN_REALM", "bharat-vistaar"))
    parser.add_argument(
        "--token-realm", default=os.environ.get("KEYCLOAK_ADMIN_TOKEN_REALM", "master")
    )
    parser.add_argument("--admin-username", default=os.environ.get("KEYCLOAK_ADMIN_USERNAME"))
    parser.add_argument("--admin-password", default=os.environ.get("KEYCLOAK_ADMIN_PASSWORD"))
    parser.add_argument(
        "--client-id",
        default=os.environ.get("KEYCLOAK_CLIENT_ID", "bharat-vistaar"),
        help="clientId of the confidential client used for email-OTP login",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    missing = [
        n
        for n, v in (
            ("--base-url / KEYCLOAK_ADMIN_BASE_URL", args.base_url),
            ("--admin-username / KEYCLOAK_ADMIN_USERNAME", args.admin_username),
            ("--admin-password / KEYCLOAK_ADMIN_PASSWORD", args.admin_password),
        )
        if not v
    ]
    if missing:
        print("Missing: " + ", ".join(missing), file=sys.stderr)
        return 2

    base = args.base_url.rstrip("/")
    token = _admin_token(base, args.token_realm, args.admin_username, args.admin_password)
    admin = f"{base}/admin/realms/{args.realm}"

    flows = _request("GET", f"{admin}/authentication/flows", token)
    flow_id = next((f["id"] for f in flows if f["alias"] == OTP_FLOW_ALIAS), None)
    if not flow_id:
        print(
            f"Realm '{args.realm}' has no '{OTP_FLOW_ALIAS}' flow. Install the email-otp\n"
            "provider and create the flow before running this.",
            file=sys.stderr,
        )
        return 1

    found = _request(
        "GET", f"{admin}/clients?clientId={urllib.parse.quote(args.client_id)}", token
    )
    if not found:
        print(f"Client '{args.client_id}' not found in realm '{args.realm}'.", file=sys.stderr)
        return 1
    client = found[0]

    current = dict(client.get("authenticationFlowBindingOverrides") or {})
    desired = {k: v for k, v in current.items() if k not in ("browser", "direct_grant")}
    # Explicit null, not omission: Keycloak MERGES this map on PUT, so a key left
    # out keeps its old value. Only an explicit null actually clears the binding.
    # Browser must end up unbound — the realm's own browser flow is what serves
    # Google SSO (proven by docs-pipeline-ui, which has no override).
    desired["browser"] = None
    desired["direct_grant"] = flow_id

    changes = []
    if current.get("browser"):
        changes.append(f"browser: {current['browser']} -> (realm default)")
    if current.get("direct_grant") != flow_id:
        changes.append(
            f"direct_grant: {current.get('direct_grant') or '(realm default)'} -> {OTP_FLOW_ALIAS}"
        )
    if not client.get("directAccessGrantsEnabled"):
        changes.append("directAccessGrantsEnabled: false -> true")
    if client.get("publicClient"):
        changes.append(
            "publicClient is true — email-OTP needs a CONFIDENTIAL client "
            "(turn Client authentication ON); not changed automatically"
        )

    print(f"client   : {args.client_id} ({client['id']})")
    print(f"realm    : {args.realm}")
    print(f"otp flow : {OTP_FLOW_ALIAS} ({flow_id})")
    if not changes:
        print("\nAlready correct — nothing to do.")
        return 0
    print("\nChanges:")
    for c in changes:
        print("  -", c)

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    _request(
        "PUT",
        f"{admin}/clients/{client['id']}",
        token,
        body={
            "authenticationFlowBindingOverrides": desired,
            "directAccessGrantsEnabled": True,
        },
    )
    print("\nApplied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
