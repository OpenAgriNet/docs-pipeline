"""Instance (tenant) access helpers for multi-instance auth."""

from __future__ import annotations

import os

from fastapi import HTTPException

from .models import AuthUser


def default_instance() -> str:
    """Fallback instance id for new docs and legacy rows without a value."""
    return (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower() or "default"


def normalize_instance(value: str | None) -> str:
    text = (value or "").strip().lower()
    return text or default_instance()


def unrestricted(user: AuthUser) -> bool:
    """True when the caller may see all instances (bypass mode or empty claim)."""
    if user.token_disabled_mode and not user.instances:
        return True
    # Master admins with no instance claim: treat as unrestricted only in bypass.
    # With Keycloak on, empty instances means no tenant access.
    return False


def allowed_instances(user: AuthUser) -> set[str] | None:
    """
    Return the set of instance ids the user may access, or None if unrestricted.
    """
    if unrestricted(user):
        return None
    return {normalize_instance(i) for i in user.instances if str(i).strip()}


def user_can_access_instance(user: AuthUser, instance: str | None) -> bool:
    allowed = allowed_instances(user)
    if allowed is None:
        return True
    if not allowed:
        return False
    return normalize_instance(instance) in allowed


def assert_instance_access(user: AuthUser, instance: str | None) -> str:
    """Raise 403 if user cannot access instance; return normalized instance id."""
    normalized = normalize_instance(instance)
    if not user_can_access_instance(user, normalized):
        raise HTTPException(403, f"No access to instance: {normalized}")
    return normalized


def assert_document_instance_access(user: AuthUser, doc: dict | None) -> dict:
    """
    Ensure the document exists and the user may access its instance.
    Missing / forbidden both return 404 to avoid leaking other tenants' ids.
    """
    if not doc:
        raise HTTPException(404, "Document not found")
    if not user_can_access_instance(user, doc.get("instance")):
        raise HTTPException(404, "Document not found")
    return doc
