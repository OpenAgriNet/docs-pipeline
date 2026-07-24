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


# Any successfully authenticated JWT holder gets at least SEARCH so the
# operator console is usable before custom realm roles are assigned.
DEFAULT_AUTHENTICATED_PERMISSIONS: frozenset[Permission] = frozenset({Permission.SEARCH})

# State-level operator: can run the document pipeline for their instance(s).
# Does NOT include platform admin settings or user management.
STATE_ADMIN_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.UPLOAD,
        Permission.REVIEW,
        Permission.PIPELINE,
        Permission.SEARCH,
    }
)

# Platform superadmin: full console + settings + user management.
SUPERADMIN_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

# Keycloak / realm role names → permissions.
# Names are matched case-insensitively after strip.
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    # Platform-wide superadmin (full access, all instances)
    "superadmin": SUPERADMIN_PERMISSIONS,
    "super_admin": SUPERADMIN_PERMISSIONS,
    "master_admin": SUPERADMIN_PERMISSIONS,  # legacy alias
    "realm-admin": SUPERADMIN_PERMISSIONS,
    # State-level admin (scoped by JWT instances claim)
    "admin": STATE_ADMIN_PERMISSIONS,
    "state_admin": STATE_ADMIN_PERMISSIONS,
    "state-admin": STATE_ADMIN_PERMISSIONS,
    # Same operational set as state admin (curators / operators)
    "content_curator": STATE_ADMIN_PERMISSIONS,
    "curator": STATE_ADMIN_PERMISSIONS,
    "operator": STATE_ADMIN_PERMISSIONS,
    # Read-only
    "viewer": frozenset({Permission.SEARCH}),
    "user": frozenset({Permission.SEARCH}),
    "reader": frozenset({Permission.SEARCH}),
    # Keycloak noise / default composites → search only
    "offline_access": frozenset({Permission.SEARCH}),
    "uma_authorization": frozenset({Permission.SEARCH}),
}

# Roles that may be assigned WITHIN a single tenant (per-tenant / group claims).
#
# This is the whitelist used by ``auth.jwt._parse_tenant_roles`` and by the
# app-side ``tenant_members`` overlay: any ``tenant_roles`` / ``groups`` entry
# naming a role OUTSIDE this set mints NO membership. ``superadmin`` /
# ``master_admin`` are deliberately EXCLUDED — they are PLATFORM-level roles
# (data-unrestricted + control plane) and must never be tenant-scoped, so a
# per-tenant claim can never smuggle in platform-wide access.
#
# ``content_curator`` shares ``state_admin``'s operational permission set (see
# ROLE_PERMISSIONS); ``viewer`` is SEARCH-only.
VALID_TENANT_ROLES: frozenset[str] = frozenset(
    {
        "state_admin",
        "content_curator",
        "viewer",
    }
)


# Realm default-role composite names vary by realm; match by prefix below.
_DEFAULT_ROLE_PREFIXES = (
    "default-roles-",
    "default_roles_",
)


def permissions_for_roles(roles: list[str] | set[str] | tuple[str, ...]) -> set[Permission]:
    """Union permissions from known roles.

    - ``superadmin`` / ``master_admin`` → all permissions
    - ``admin`` → state-level: upload, review, pipeline, search
    - Unknown / default realm roles → baseline SEARCH only
    """
    granted: set[Permission] = set()

    for role in roles:
        key = (role or "").strip().lower()
        if not key:
            continue

        if key in ROLE_PERMISSIONS:
            granted.update(ROLE_PERMISSIONS[key])
            continue

        # default-roles-<realm> composites
        if any(key.startswith(prefix) for prefix in _DEFAULT_ROLE_PREFIXES):
            granted.update(DEFAULT_AUTHENTICATED_PERMISSIONS)
            continue

    if not granted:
        # Valid token but no mapped roles → baseline access.
        granted.update(DEFAULT_AUTHENTICATED_PERMISSIONS)

    return granted
