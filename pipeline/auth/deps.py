"""FastAPI dependencies for identity and permission checks."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Annotated, Any, Callable

from fastapi import Depends, HTTPException, Request

from .config import load_auth_config
from .jwt import claims_to_user, decode_and_validate_token
from .models import AuthUser, local_bypass_user
from .permissions import Permission


def _bearer_token(request: Request, *, required: bool = True) -> str | None:
    header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not header:
        if required:
            raise HTTPException(401, "Missing Bearer token")
        return None
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        if required:
            raise HTTPException(401, "Authorization header must be: Bearer <token>")
        return None
    return parts[1].strip()


def _unverified_claims(token: str) -> dict[str, Any] | None:
    """Decode JWT payload without signature verification (display/dev only)."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        # pad base64url
        padded = payload + "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _bypass_user_with_optional_jwt(token: str | None) -> AuthUser:
    """Full-access bypass user, enriched with name/email/roles from JWT when present."""
    base = local_bypass_user()
    if not token:
        return base
    claims = _unverified_claims(token)
    if not claims:
        return base
    try:
        from_token = claims_to_user(claims)
    except Exception:
        return base
    # Keep unrestricted permissions from bypass; overlay identity + roles for UI.
    return AuthUser(
        user_id=from_token.user_id or base.user_id,
        username=from_token.username or base.username,
        email=from_token.email or base.email,
        roles=from_token.roles or base.roles,
        permissions=base.permissions,
        instances=from_token.instances or base.instances,
        envs=from_token.envs or base.envs,
        token_disabled_mode=True,
    )


async def get_current_user(request: Request) -> AuthUser:
    """Resolve the caller. When AUTH_DISABLED=true, returns a local bypass user.

    If a Bearer JWT is still sent in bypass mode, name/email/roles are taken
    from the token so the UI can show the real SSO identity.
    """
    config = load_auth_config()
    if config.disabled:
        token = _bearer_token(request, required=False)
        if not token:
            token = (request.query_params.get("access_token") or "").strip() or None
        return _bypass_user_with_optional_jwt(token)

    token = _bearer_token(request, required=True)
    if not token:
        # Fallback for browser element loads (PDF <embed>, export <a href>) that
        # cannot send an Authorization header: accept ?access_token=<jwt>.
        token = (request.query_params.get("access_token") or "").strip() or None
    if not token:
        raise HTTPException(401, "Missing Bearer token")
    # JWKS fetch/refresh is sync and can block; keep the event loop free.
    return await asyncio.to_thread(decode_and_validate_token, token, config)


def require_permission(permission: Permission | str) -> Callable[..., AuthUser]:
    """Dependency factory: require a logged-in user with the given permission."""
    needed = permission if isinstance(permission, Permission) else Permission(str(permission))

    async def _checker(user: Annotated[AuthUser, Depends(get_current_user)]) -> AuthUser:
        if not user.has_permission(needed):
            raise HTTPException(
                403,
                f"Missing permission: {needed.value}",
            )
        return user

    return _checker


# Convenience aliases for route annotations
RequireUpload = Annotated[AuthUser, Depends(require_permission(Permission.UPLOAD))]
RequireReview = Annotated[AuthUser, Depends(require_permission(Permission.REVIEW))]
RequirePipeline = Annotated[AuthUser, Depends(require_permission(Permission.PIPELINE))]
RequireSearch = Annotated[AuthUser, Depends(require_permission(Permission.SEARCH))]
RequireAdmin = Annotated[AuthUser, Depends(require_permission(Permission.ADMIN))]
RequireManageUsers = Annotated[AuthUser, Depends(require_permission(Permission.MANAGE_USERS))]
CurrentUser = Annotated[AuthUser, Depends(get_current_user)]
