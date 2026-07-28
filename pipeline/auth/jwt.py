"""JWT validation against Keycloak JWKS."""

from __future__ import annotations

from typing import Any

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

from .config import AuthConfig
from .groups import ROLE_SUPER_ADMIN, extract_groups_claim, normalize_role, parse_group_paths
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
    """Map Keycloak JWT claims → AuthUser.

    Preferred source of tenant + role is the ``groups`` claim (full paths):
    ``/states/MH/contributor``, ``/global/super-admin``.

    Fallback (legacy tokens): realm roles + multivalued ``instances`` /
    ``tenants`` user attributes.
    """
    raw_realm_roles = _extract_roles(claims)
    group_paths = extract_groups_claim(claims)
    group_access = parse_group_paths(group_paths)

    # Canonical product roles from groups first; realm roles fill gaps / legacy.
    product_roles: set[str] = set(group_access.roles)
    for raw in raw_realm_roles:
        canon = normalize_role(raw)
        if canon:
            product_roles.add(canon)
        elif raw and not raw.lower().startswith("default-roles-"):
            # Keep unmapped realm roles for display; permissions map may ignore them.
            product_roles.add(raw.strip().lower())

    if group_access.is_super_admin:
        product_roles.add(ROLE_SUPER_ADMIN)

    roles = sorted(product_roles)

    # Instances: prefer group-derived states; fall back to attribute claims.
    if group_access.is_super_admin:
        instances: list[str] = []
    elif group_access.state_roles:
        instances = group_access.instances
    else:
        instances = [
            i.strip().lower()
            for i in _extract_string_list(claims, "instances", "tenants", "tenant")
            if str(i).strip()
        ]

    return AuthUser(
        user_id=str(claims.get("sub") or claims.get("user_id") or ""),
        username=str(claims.get("preferred_username") or claims.get("username") or ""),
        email=str(claims.get("email") or ""),
        roles=roles,
        permissions=permissions_for_roles(roles),
        instances=instances,
        envs=_extract_string_list(claims, "envs", "environments", "env"),
        groups=group_access.groups or group_paths,
        state_roles=dict(group_access.state_roles),
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
        options: dict[str, Any] = {"require": ["exp"]}
        decode_kwargs: dict[str, Any] = {
            "algorithms": ["RS256"],
            "issuer": config.keycloak_issuer,
            "leeway": config.jwt_leeway_seconds,
            "options": options,
        }
        if config.keycloak_audience:
            decode_kwargs["audience"] = config.keycloak_audience
        else:
            options["verify_aud"] = False

        claims = jwt.decode(token, signing_key.key, **decode_kwargs)
    except jwt.PyJWTError as exc:
        raise HTTPException(401, f"Invalid token: {exc}") from exc
    except Exception as exc:
        raise HTTPException(401, f"Unable to validate token: {exc}") from exc

    user = claims_to_user(claims)
    if not user.user_id:
        raise HTTPException(401, "Token missing subject")
    return user
