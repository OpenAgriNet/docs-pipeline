"""JWT validation against Keycloak JWKS."""

from __future__ import annotations

import os
from typing import Any

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

from .config import AuthConfig
from .models import AuthUser
from .permissions import permissions_for_roles

# Per-tenant roles we accept from a token's ``tenant_roles`` / ``groups`` claim.
# Anything else (e.g. a spoofed ``/x/superuser`` group path, or an unknown role
# name) is dropped so it can never mint tenant membership or a role. Realm-level
# roles like ``master_admin`` are handled separately (see ``_extract_realm_roles``)
# and are intentionally NOT valid per-tenant roles.
KNOWN_TENANT_ROLES = frozenset({"admin", "content_curator", "viewer"})

_jwks_clients: dict[str, PyJWKClient] = {}


def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    client = _jwks_clients.get(jwks_url)
    if client is None:
        client = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        _jwks_clients[jwks_url] = client
    return client


def clear_jwks_cache() -> None:
    _jwks_clients.clear()


def _extract_realm_roles(claims: dict[str, Any]) -> list[str]:
    """Realm-level roles ONLY — ``realm_access.roles``.

    Distinct from :func:`_extract_roles`, which additionally folds in
    ``resource_access`` client roles and a flat ``roles`` claim. The platform-admin
    (instance-unrestricted) check keys off realm roles alone, so a *client* role
    named ``master_admin`` in ``resource_access`` (or a flat ``roles`` entry) must
    never appear here and thus can never grant platform-wide access.
    """
    roles: set[str] = set()
    realm = claims.get("realm_access") or {}
    if isinstance(realm, dict):
        for role in realm.get("roles") or []:
            if isinstance(role, str) and role.strip():
                roles.add(role.strip())
    return sorted(roles)


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


def _parse_tenant_roles(claims: dict[str, Any]) -> dict[str, set[str]]:
    """Parse per-tenant roles from the ``tenant_roles`` object and/or ``groups`` list.

    ``tenant_roles`` = ``{"<instance>": ["admin"|"content_curator"|"viewer", ...]}``.
    ``groups`` = list of Keycloak group paths ``/<instance>/<role>`` (e.g.
    ``["/tenant-a/content_curator", "/tenant-b/viewer"]``) — parsed into the same
    ``{instance: {role, ...}}`` shape. Both may be present; results are merged.
    Instance ids and role names are normalized to lowercase.

    Hardening (defense-in-depth): only role names in :data:`KNOWN_TENANT_ROLES`
    are accepted; an unknown role segment (e.g. a spoofed ``/x/superuser`` group)
    is dropped and mints NO membership. When ``KEYCLOAK_TENANT_GROUP_PREFIX`` is
    set, only ``groups`` paths under that prefix are considered (paths outside it
    are ignored); when unset, group parsing is unchanged (back-compat).
    """
    result: dict[str, set[str]] = {}

    def _add(inst: str, role: str) -> None:
        key = (inst or "").strip().lower()
        name = (role or "").strip().lower()
        if key and name and name in KNOWN_TENANT_ROLES:
            result.setdefault(key, set()).add(name)

    raw = claims.get("tenant_roles")
    if isinstance(raw, dict):
        for inst, roles in raw.items():
            if isinstance(roles, str):
                role_iter = [roles]
            elif isinstance(roles, (list, tuple)):
                role_iter = list(roles)
            else:
                continue
            for role in role_iter:
                _add(str(inst or ""), str(role or ""))

    prefix = (os.environ.get("KEYCLOAK_TENANT_GROUP_PREFIX") or "").strip()
    norm_prefix = "/" + prefix.strip("/") if prefix else ""
    groups = claims.get("groups")
    if isinstance(groups, (list, tuple)):
        for path in groups:
            if not isinstance(path, str):
                continue
            remainder = path
            if norm_prefix:
                norm_path = "/" + path.strip("/")
                if not (norm_path == norm_prefix or norm_path.startswith(norm_prefix + "/")):
                    # Group path is outside the configured tenant-group prefix.
                    continue
                remainder = norm_path[len(norm_prefix):]
            parts = [p.strip() for p in remainder.split("/") if p.strip()]
            if len(parts) < 2:
                continue
            _add(parts[0], parts[1])

    return result


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
    # ``realm_roles`` drives the platform-admin (instance-unrestricted) gate and is
    # STRICTLY the realm_access roles. ``union_roles`` is the wider any-instance view
    # (realm + resource_access + flat ``roles``) used for the per-tenant/compat logic.
    realm_roles = _extract_realm_roles(claims)
    union_roles = _extract_roles(claims)
    tenant_roles = _parse_tenant_roles(claims)
    legacy_instances = _extract_string_list(claims, "instances", "tenants", "tenant")

    # ``instances`` = keys(tenant_roles) unioned with any legacy flat ``instances``
    # claim (back-compat). When no tenant_roles/groups are present, preserve the
    # legacy list verbatim (order/case) so existing behaviour is unchanged.
    if tenant_roles:
        instances = list(tenant_roles.keys())
        seen = {i.lower() for i in instances}
        for inst in legacy_instances:
            if inst.lower() not in seen:
                instances.append(inst)
                seen.add(inst.lower())
    else:
        instances = legacy_instances

    # Flat ``roles`` / ``permissions`` are the any-instance view: realm/resource
    # roles unioned with every per-tenant role, so ``has_permission`` answers
    # "does the caller hold this permission in *any* tenant" (compat gate).
    flat_roles = set(union_roles)
    for role_set in tenant_roles.values():
        flat_roles.update(role_set)
    roles = sorted(flat_roles)

    return AuthUser(
        user_id=str(claims.get("sub") or claims.get("user_id") or ""),
        username=str(claims.get("preferred_username") or claims.get("username") or ""),
        email=str(claims.get("email") or ""),
        roles=roles,
        realm_roles=realm_roles,
        permissions=permissions_for_roles(roles),
        instances=instances,
        envs=_extract_string_list(claims, "envs", "environments", "env"),
        tenant_roles=tenant_roles,
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
