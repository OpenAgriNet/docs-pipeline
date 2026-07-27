"""Instance (tenant / state) access helpers — Plan 2 (Keycloak + document.instance).

Access who-sees-what-state comes from the JWT (Keycloak groups). The database
only stores which state a *document* belongs to (``documents.instance``).

Plan 2 conventions:
  - State codes: lowercase (``mh``, ``up``, …) matching Keycloak ``/states/MH/...``
  - Portal / BV platform docs: ``bv`` (Bharat Vistaar)
  - Super admin (``/global/super-admin``): unrestricted instances
"""

from __future__ import annotations

import os

from fastapi import HTTPException

from .models import AuthUser

# Bharat Vistaar portal / platform-wide documents (not a state).
PORTAL_INSTANCE = "bv"


def default_instance() -> str:
    """Fallback instance id for new docs and legacy rows without a value."""
    return (os.environ.get("DEFAULT_INSTANCE") or "default").strip().lower() or "default"


def normalize_instance(value: str | None) -> str:
    text = (value or "").strip().lower()
    return text or default_instance()


def unrestricted(user: AuthUser) -> bool:
    """True when the caller may see all instances (super admin / local bypass)."""
    return user.is_instance_unrestricted()


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


def resolve_create_instance(user: AuthUser, requested: str | None = None) -> str:
    """Pick document ``instance`` at create/upload time (Plan 2).

    - Super admin / bypass: use requested, else portal ``bv``, else DEFAULT_INSTANCE.
    - Single-state user: that state (requested must match if provided).
    - Multi-state user: must pass ``requested`` among allowed states.
    - No states in token: 403 (Keycloak group missing).
    """
    requested_norm = (requested or "").strip().lower() or None

    if unrestricted(user):
        if requested_norm:
            return normalize_instance(requested_norm)
        # Prefer portal tag for platform operators when nothing chosen.
        portal = (os.environ.get("PORTAL_INSTANCE") or PORTAL_INSTANCE).strip().lower()
        return normalize_instance(portal or default_instance())

    allowed = allowed_instances(user) or set()
    if not allowed:
        raise HTTPException(
            403,
            "No state assigned. Join a Keycloak group such as /states/MH/contributor.",
        )

    if requested_norm:
        if requested_norm not in allowed:
            raise HTTPException(403, f"No access to instance: {requested_norm}")
        return requested_norm

    if len(allowed) == 1:
        return next(iter(allowed))

    raise HTTPException(
        400,
        "instance is required when you have multiple states; "
        f"choose one of: {', '.join(sorted(allowed))}",
    )


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
