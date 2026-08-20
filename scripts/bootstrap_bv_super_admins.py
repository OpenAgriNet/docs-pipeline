#!/usr/bin/env python3
"""Ensure BV Super Admin group + assign super_admin to given emails.

Uses Keycloak Admin API (KEYCLOAK_ADMIN_* / KEYCLOAK_ISSUER from env).

  python scripts/bootstrap_bv_super_admins.py
  python scripts/bootstrap_bv_super_admins.py --email someone@example.com
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow `python scripts/...` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load root .env if present (does not override existing env)
_env_path = ROOT / ".env"
if _env_path.is_file():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        os.environ.setdefault(key, val)

from pipeline.auth.keycloak_admin import (  # noqa: E402
    ensure_access_group,
    load_keycloak_admin_config,
    provision_user,
    require_admin_config,
)

DEFAULT_SUPER_ADMINS = [
    ("akshat.rana@kenpath.io", "Akshat", "Rana"),
    ("akshatrana262@gmail.com", "Akshat", "Rana"),
    ("akshatrana033@gmail.com", "Akshat", "Rana"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap BV Super Admins in Keycloak")
    parser.add_argument(
        "--email",
        action="append",
        dest="emails",
        help="Extra email to grant super_admin (repeatable)",
    )
    parser.add_argument(
        "--ensure-state-groups",
        action="store_true",
        help="Also ensure /states/{STATE}/admin and /view for default states",
    )
    args = parser.parse_args()

    cfg = load_keycloak_admin_config()
    if not cfg.configured:
        print(
            "ERROR: Keycloak admin not configured. Set KEYCLOAK_ADMIN_USERNAME, "
            "KEYCLOAK_ADMIN_PASSWORD, and KEYCLOAK_ISSUER or KEYCLOAK_ADMIN_BASE_URL.",
            file=sys.stderr,
        )
        return 1

    print(f"Keycloak admin → {cfg.base_url} realm={cfg.realm}")

    # Ensure super-admin group + realm role exist
    path, _ = ensure_access_group(cfg, access_type="super_admin", state=None, role=None)
    print(f"Ensured group {path}")

    if args.ensure_state_groups:
        from pipeline.auth.keycloak_admin import DEFAULT_STATE_CODES

        for code in DEFAULT_STATE_CODES[:12]:  # core states
            for role in ("state_admin", "state_view"):
                p, _ = ensure_access_group(
                    cfg, access_type="state", state=code, role=role
                )
                print(f"  Ensured {p}")

    targets = list(DEFAULT_SUPER_ADMINS)
    for email in args.emails or []:
        targets.append((email.strip().lower(), "", ""))

    # Dedupe by email
    seen: set[str] = set()
    unique = []
    for email, first, last in targets:
        e = email.strip().lower()
        if not e or e in seen:
            continue
        seen.add(e)
        unique.append((e, first, last))

    ok = 0
    for email, first, last in unique:
        try:
            result = provision_user(
                email=email,
                first_name=first,
                last_name=last,
                access_type="super_admin",
                enabled=True,
            )
            print(
                f"OK  {email}  created={result.get('created')}  "
                f"groups={result.get('groups')}  roles={result.get('roles')}"
            )
            ok += 1
        except Exception as exc:
            print(f"FAIL {email}: {exc}", file=sys.stderr)

    print(f"Done: {ok}/{len(unique)} super admins provisioned")
    return 0 if ok == len(unique) else 2


if __name__ == "__main__":
    raise SystemExit(main())
