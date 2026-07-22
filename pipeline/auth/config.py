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
    Do not flip this until the maintainer UI sends Authorization headers.
    """

    disabled: bool
    keycloak_issuer: str
    keycloak_audience: str
    keycloak_jwks_url: str
    # Clock-skew tolerance when validating exp/nbf (seconds).
    jwt_leeway_seconds: int = 30


def load_auth_config() -> AuthConfig:
    issuer = (os.environ.get("KEYCLOAK_ISSUER") or "").rstrip("/")
    jwks_url = (os.environ.get("KEYCLOAK_JWKS_URL") or "").strip()
    if not jwks_url and issuer:
        jwks_url = f"{issuer}/protocol/openid-connect/certs"

    leeway_raw = (os.environ.get("KEYCLOAK_JWT_LEEWAY_SECONDS") or "30").strip()
    try:
        leeway = max(0, int(leeway_raw))
    except ValueError:
        leeway = 30

    # Empty KEYCLOAK_AUDIENCE disables audience checks (common for public SPA
    # clients where `aud` is "account" or a multi-value claim). Only set it when
    # tokens always include a stable audience you want to enforce.
    raw_audience = os.environ.get("KEYCLOAK_AUDIENCE")
    if raw_audience is None:
        audience = ""
    else:
        audience = raw_audience.strip()

    return AuthConfig(
        disabled=_env_bool("AUTH_DISABLED", True),
        keycloak_issuer=issuer,
        keycloak_audience=audience,
        keycloak_jwks_url=jwks_url,
        jwt_leeway_seconds=leeway,
    )


def validate_auth_config(config: AuthConfig) -> None:
    """Fail startup early when enabled auth cannot validate Keycloak tokens."""
    if config.disabled:
        return

    missing: list[str] = []
    if not config.keycloak_issuer:
        missing.append("KEYCLOAK_ISSUER")
    if not config.keycloak_jwks_url:
        missing.append("KEYCLOAK_JWKS_URL")
    if missing:
        raise RuntimeError(
            "AUTH_DISABLED=false requires Keycloak configuration: "
            + ", ".join(missing)
        )
