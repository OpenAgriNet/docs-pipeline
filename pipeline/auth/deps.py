"""FastAPI dependencies for identity and permission checks."""

from __future__ import annotations

import asyncio
from typing import Annotated, Callable

from fastapi import Depends, HTTPException, Request

from .config import load_auth_config
from .jwt import decode_and_validate_token
from .models import AuthUser, local_bypass_user
from .permissions import Permission


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization") or request.headers.get("authorization")
    if not header:
        return None
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(401, "Authorization header must be: Bearer <token>")
    return parts[1].strip()


async def get_current_user(request: Request) -> AuthUser:
    """Resolve the caller. When AUTH_DISABLED=true, returns a local bypass user."""
    config = load_auth_config()
    if config.disabled:
        return local_bypass_user()

    token = _bearer_token(request)
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
