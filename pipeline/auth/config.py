"""Auth configuration loaded from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AuthConfig:
    """
    AUTH_DISABLED=true (default): accept a synthetic local admin user so
    existing deploys and tests keep working until Keycloak is wired.

    AUTH_DISABLED=false: require a valid Bearer JWT from Keycloak.
    """

    disabled: bool
    keycloak_issuer: str
    keycloak_audience: str
    keycloak_jwks_url: str
    # Optional: if set, only these roles grant any access (still mapped via ROLE_PERMISSIONS).
    required_role_prefix: str = ""


def load_auth_config() -> AuthConfig:
    issuer = (os.environ.get("KEYCLOAK_ISSUER") or "").rstrip("/")
    jwks_url = (os.environ.get("KEYCLOAK_JWKS_URL") or "").strip()
    if not jwks_url and issuer:
        jwks_url = f"{issuer}/protocol/openid-connect/certs"

    return AuthConfig(
        disabled=_env_bool("AUTH_DISABLED", True),
        keycloak_issuer=issuer,
        keycloak_audience=(os.environ.get("KEYCLOAK_AUDIENCE") or "docs-pipeline-api").strip(),
        keycloak_jwks_url=jwks_url,
        required_role_prefix=(os.environ.get("KEYCLOAK_ROLE_PREFIX") or "").strip(),
    )
