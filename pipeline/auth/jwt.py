"""JWT validation against Keycloak JWKS."""

from __future__ import annotations

import os
from typing import Any

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

from .config import AuthConfig
from .models import AuthUser
from .permissions import VALID_TENANT_ROLES, permissions_for_roles

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
    ``resource_access`` client roles and a flat ``roles`` claim. Captured for
    parity with ``main``; bh-main's platform-admin gate keeps keying off the
    wider ``roles`` union (``AuthUser.is_superadmin``), so this is informational
    on bh-main for now.
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


def _parse_tenant_roles(claims: dict[str, Any]) -> dict[str, set[str]]:
    """Parse per-tenant roles from the token — VERSION-TOLERANT (see below).

    Accepts, and merges, every shape a version-unknown Keycloak might emit —
    this must NOT depend on any KC-26-only claim (no Organizations claim, no
    orgs API):

    (a) a ``tenant_roles`` object claim ``{"<instance>": ["state_admin", ...]}``;
    (b) ``groups`` path claims ``/<instance>/<role>`` (e.g.
        ``["/tenant-a/content_curator", "/tenant-b/viewer"]``). An optional
        ``KEYCLOAK_TENANT_GROUP_PREFIX`` env names a parent group segment to
        strip first (``/<prefix>/<instance>/<role>``); when unset, paths are
        parsed as ``/<instance>/<role>`` (back-compat);
    (c) flat realm roles alone → returns ``{}`` so the model falls back to
        legacy flat-claim mode (flat ``roles`` apply across claimed instances).

    Only role names in :data:`VALID_TENANT_ROLES` mint a membership; any other
    role (including ``superadmin`` / ``master_admin``, which are platform-level)
    is ignored, so a per-tenant claim can never grant platform-wide access.
    Instance ids and role names are normalized to lowercase.
    """
    result: dict[str, set[str]] = {}

    def _add(inst: Any, role: Any) -> None:
        i = str(inst or "").strip().lower()
        r = str(role or "").strip().lower()
        if not i or r not in VALID_TENANT_ROLES:
            return
        result.setdefault(i, set()).add(r)

    # (a) ``tenant_roles`` object claim.
    raw = claims.get("tenant_roles")
    if isinstance(raw, dict):
        for inst, roles in raw.items():
            if isinstance(roles, str):
                role_iter: list[Any] = [roles]
            elif isinstance(roles, (list, tuple, set)):
                role_iter = list(roles)
            else:
                continue
            for role in role_iter:
                _add(inst, role)

    # (b) ``groups`` path claims, optionally nested under a configured prefix.
    prefix = (os.environ.get("KEYCLOAK_TENANT_GROUP_PREFIX") or "").strip().strip("/").lower()
    groups = claims.get("groups")
    if isinstance(groups, (list, tuple)):
        for path in groups:
            if not isinstance(path, str):
                continue
            parts = [p.strip() for p in path.split("/") if p.strip()]
            if prefix:
                # Only consider groups under the configured parent segment.
                if not parts or parts[0].lower() != prefix:
                    continue
                parts = parts[1:]
            if len(parts) < 2:
                continue
            _add(parts[0], parts[1])

    return result


def _overlay_app_memberships(
    user_id: str,
    email: str,
    username: str,
    tenant_roles: dict[str, set[str]],
) -> dict[str, set[str]]:
    """UNION the app-side ``tenant_members`` store into token-derived roles.

    D2: the app-side membership store is the AUTHORITATIVE baseline; token
    claims layer ADDITIVELY on top (union). Best-effort by design — the ``db``
    module is imported lazily (avoids an import cycle) and every call is wrapped
    so a DB hiccup, a not-yet-migrated schema, or a missing helper NEVER blocks
    authentication. Only :data:`VALID_TENANT_ROLES` entries are honoured.
    """
    if not user_id:
        return tenant_roles
    try:
        from pipeline import db  # lazy import to avoid an import cycle
    except Exception:
        return tenant_roles

    # Record identity for the admin console's member picker (never fatal).
    try:
        db.upsert_seen_user(user_id=user_id, email=email, username=username)
    except Exception:
        pass

    try:
        stored = db.get_tenant_roles_for_user(user_id)
    except Exception:
        stored = None

    if isinstance(stored, dict):
        for inst, roles in stored.items():
            key = str(inst or "").strip().lower()
            if not key:
                continue
            if isinstance(roles, str):
                role_iter: list[Any] = [roles]
            elif isinstance(roles, (list, tuple, set)):
                role_iter = list(roles)
            else:
                continue
            bucket = tenant_roles.setdefault(key, set())
            for role in role_iter:
                name = str(role or "").strip().lower()
                if name in VALID_TENANT_ROLES:
                    bucket.add(name)

    return tenant_roles


def claims_to_user(claims: dict[str, Any]) -> AuthUser:
    # ``realm_roles`` = realm_access only (parity w/ main); ``union_roles`` is the
    # wider any-instance view (realm + resource_access + flat ``roles``) that
    # drives ``is_superadmin`` and the compat ``has_permission`` gate.
    realm_roles = _extract_realm_roles(claims)
    union_roles = _extract_roles(claims)
    tenant_roles = _parse_tenant_roles(claims)
    legacy_instances = _extract_string_list(claims, "instances", "tenants", "tenant")

    user_id = str(claims.get("sub") or claims.get("user_id") or "")
    username = str(claims.get("preferred_username") or claims.get("username") or "")
    email = str(claims.get("email") or "")

    # D2 overlay: app-side membership store is the authoritative baseline, token
    # claims add on top (union). This is the single seam where AuthUser is built
    # from real-token claims (decode_and_validate_token runs it inside a worker
    # thread, so the sync sqlite read is safe). Best-effort inside the helper.
    tenant_roles = _overlay_app_memberships(user_id, email, username, tenant_roles)

    # ``instances`` = keys(tenant_roles) ∪ legacy flat ``instances`` claim
    # (back-compat). With NO tenant_roles (legacy flat-claim mode), preserve the
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

    # Flat ``roles`` / ``permissions`` = any-instance view: realm/resource/flat
    # roles unioned with every per-tenant role, so ``has_permission`` answers
    # "holds this permission in *any* tenant" (compat gate). In legacy flat-claim
    # mode tenant_roles is empty, so ``roles`` == union_roles exactly as before.
    flat_roles = set(union_roles)
    for role_set in tenant_roles.values():
        flat_roles.update(role_set)
    roles = sorted(flat_roles)

    return AuthUser(
        user_id=user_id,
        username=username,
        email=email,
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
