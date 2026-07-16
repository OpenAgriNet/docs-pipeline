"""JWT validation against Keycloak JWKS."""

from __future__ import annotations

from typing import Any

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

from .config import AuthConfig
from .models import AuthUser
from .permissions import permissions_for_roles

_jwks_clients: dict[str, PyJWKClient] = {}


def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    client = _jwks_clients.get(jwks_url)
    if client is None:
        client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        _jwks_clients[jwks_url] = client
    return client


def clear_jwks_cache() -> None:
    _jwks_clients.clear()


def _extract_roles(claims: dict[str, Any]) -> list[str]:
    roles: set[str] = set()
    realm = claims.get("realm_access") or {}
    for role in realm.get("roles") or []:
        if isinstance(role, str) and role.strip():
            roles.add(role.strip())

    resource_access = claims.get("resource_access") or {}
    if isinstance(resource_access, dict):
        for client_data in resource_access.values():
            if not isinstance(client_data, dict):
                continue
            for role in client_data.get("roles") or []:
                if isinstance(role, str) and role.strip():
                    roles.add(role.strip())

    for role in claims.get("roles") or []:
        if isinstance(role, str) and role.strip():
            roles.add(role.strip())

    return sorted(roles)


def _extract_string_list(claims: dict[str, Any], *keys: str) -> list[str]:
    for key in keys:
        raw = claims.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
            if parts:
                return parts
        if isinstance(raw, (list, tuple)):
            return [str(item).strip() for item in raw if str(item).strip()]
    return []


def claims_to_user(claims: dict[str, Any]) -> AuthUser:
    roles = _extract_roles(claims)
    return AuthUser(
        user_id=str(claims.get("sub") or claims.get("user_id") or ""),
        username=str(claims.get("preferred_username") or claims.get("username") or ""),
        email=str(claims.get("email") or ""),
        roles=roles,
        permissions=permissions_for_roles(roles),
        instances=_extract_string_list(claims, "instances", "tenants", "tenant"),
        envs=_extract_string_list(claims, "envs", "environments", "env"),
        token_disabled_mode=False,
    )


def decode_and_validate_token(token: str, config: AuthConfig) -> AuthUser:
    if not config.keycloak_issuer or not config.keycloak_jwks_url:
        raise HTTPException(
            401,
            "Auth is enabled but KEYCLOAK_ISSUER / KEYCLOAK_JWKS_URL are not configured",
        )

    try:
        signing_key = _get_jwks_client(config.keycloak_jwks_url).get_signing_key_from_jwt(token)
        decode_kwargs: dict[str, Any] = {
            "algorithms": ["RS256"],
            "issuer": config.keycloak_issuer,
        }
        if config.keycloak_audience:
            decode_kwargs["audience"] = config.keycloak_audience
        else:
            decode_kwargs["options"] = {"verify_aud": False}

        claims = jwt.decode(token, signing_key.key, **decode_kwargs)
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"Invalid token: {exc}") from exc
    except Exception as exc:
        raise HTTPException(401, f"Unable to validate token: {exc}") from exc

    user = claims_to_user(claims)
    if not user.user_id:
        raise HTTPException(401, "Token missing subject")
    return user
