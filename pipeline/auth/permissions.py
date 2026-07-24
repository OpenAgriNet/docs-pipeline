"""Named API permissions and default role → permission mapping."""

from __future__ import annotations

from enum import Enum


class Permission(str, Enum):
    """Capability names enforced by API dependencies.

    Uses ``(str, Enum)`` instead of ``enum.StrEnum`` so the API image
    (Python 3.10) can import this module at startup.
    """

    UPLOAD = "upload"
    REVIEW = "review"
    PIPELINE = "pipeline"
    SEARCH = "search"
    ADMIN = "admin"
    MANAGE_USERS = "manage_users"


# Keycloak / realm role names → permissions (v1).
#
# ``master_admin`` is the CONTROL-PLANE super-admin: it manages the tenant
# registry (create/suspend/delete tenants, provision tenant admins) but holds
# **no data permissions** of its own. Its authority comes from the realm-role
# platform-admin gate (see ``AuthUser.is_platform_admin`` /
# ``require_platform_admin``), not from this data-permission map — so a pure
# master_admin can never read or write any tenant's documents, chunks, search,
# runs, artifacts, settings, or indexes. Data access is exclusively per-tenant
# (``admin`` / ``content_curator`` / ``viewer`` assigned within a tenant).
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "master_admin": frozenset(),
    "admin": frozenset(Permission),
    "content_curator": frozenset(
        {
            Permission.UPLOAD,
            Permission.REVIEW,
            Permission.PIPELINE,
            Permission.SEARCH,
        }
    ),
    "viewer": frozenset({Permission.SEARCH}),
}


def permissions_for_roles(roles: list[str] | set[str] | tuple[str, ...]) -> set[Permission]:
    """Union permissions from known roles (unknown roles ignored)."""
    granted: set[Permission] = set()
    for role in roles:
        key = (role or "").strip().lower()
        if not key:
            continue
        granted.update(ROLE_PERMISSIONS.get(key, ()))
    return granted
