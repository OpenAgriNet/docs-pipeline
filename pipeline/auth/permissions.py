"""Named API permissions and role → permission mapping.

Product roles (Keycloak groups / realm roles):

- ``super_admin``  — Bharat Vistaar platform super admin (all states)
- ``state_admin``  — full access within assigned state(s)
- ``state_view``   — view-only within assigned state(s)

Legacy aliases (contributor → state_admin, reviewer → state_view) remain
accepted so existing Keycloak group leaves keep working.
"""

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
    # State-admin destructive actions (enforced with ownership / tenancy checks).
    DELETE_OWN = "delete_own"


class UserRole(str, Enum):
    """Canonical product roles (mirror Keycloak group leaves / realm roles)."""

    SUPER_ADMIN = "super_admin"
    STATE_ADMIN = "state_admin"
    STATE_VIEW = "state_view"


# Any successfully authenticated JWT holder gets at least SEARCH so the
# operator console is usable before custom realm roles are assigned.
DEFAULT_AUTHENTICATED_PERMISSIONS: frozenset[Permission] = frozenset({Permission.SEARCH})

# State view: read-only console for assigned state(s).
STATE_VIEW_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.SEARCH,
    }
)

# State admin: full operational access within assigned state(s).
# Prod promote / user management stay platform-only (super_admin).
STATE_ADMIN_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.UPLOAD,
        Permission.REVIEW,
        Permission.PIPELINE,
        Permission.SEARCH,
        Permission.DELETE_OWN,
    }
)

# Platform super admin (Bharat Vistaar): full console + settings + users + prod.
SUPERADMIN_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

# Keycloak / realm role names → permissions.
# Names are matched case-insensitively after strip / alias normalization.
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    # Product roles
    UserRole.SUPER_ADMIN.value: SUPERADMIN_PERMISSIONS,
    UserRole.STATE_ADMIN.value: STATE_ADMIN_PERMISSIONS,
    UserRole.STATE_VIEW.value: STATE_VIEW_PERMISSIONS,
    # Super-admin aliases
    "superadmin": SUPERADMIN_PERMISSIONS,
    "super-admin": SUPERADMIN_PERMISSIONS,
    "master_admin": SUPERADMIN_PERMISSIONS,
    "master-admin": SUPERADMIN_PERMISSIONS,
    "realm-admin": SUPERADMIN_PERMISSIONS,
    # State admin aliases (incl. legacy contributor)
    "admin": STATE_ADMIN_PERMISSIONS,
    "state-admin": STATE_ADMIN_PERMISSIONS,
    "contributor": STATE_ADMIN_PERMISSIONS,
    "content_curator": STATE_ADMIN_PERMISSIONS,
    "curator": STATE_ADMIN_PERMISSIONS,
    "operator": STATE_ADMIN_PERMISSIONS,
    # State view aliases (incl. legacy reviewer)
    "view": STATE_VIEW_PERMISSIONS,
    "state-view": STATE_VIEW_PERMISSIONS,
    "viewer": STATE_VIEW_PERMISSIONS,
    "reviewer": STATE_VIEW_PERMISSIONS,
    "reader": STATE_VIEW_PERMISSIONS,
    "user": DEFAULT_AUTHENTICATED_PERMISSIONS,
    # Keycloak noise / default composites → search only
    "offline_access": frozenset({Permission.SEARCH}),
    "uma_authorization": frozenset({Permission.SEARCH}),
}

# Realm default-role composite names vary by realm; match by prefix below.
_DEFAULT_ROLE_PREFIXES = (
    "default-roles-",
    "default_roles_",
)


def permissions_for_roles(roles: list[str] | set[str] | tuple[str, ...]) -> set[Permission]:
    """Union permissions from known roles.

    - ``super_admin`` → all permissions
    - ``state_admin`` → upload, review, pipeline, search, delete_own
    - ``state_view`` → search only
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
