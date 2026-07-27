"""Named API permissions and role → permission mapping.

Product roles (Keycloak groups / realm roles):

- ``super_admin``  — full access, all states, user management
- ``contributor``  — upload, edit/delete own, view all in state, approve own
- ``reviewer``     — edit/review/approve in state; no upload, no delete
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
    # Contributor-only destructive actions (enforced with ownership checks).
    DELETE_OWN = "delete_own"


class UserRole(str, Enum):
    """Canonical product roles (mirror Keycloak group leaves / realm roles)."""

    SUPER_ADMIN = "super_admin"
    CONTRIBUTOR = "contributor"
    REVIEWER = "reviewer"


# Any successfully authenticated JWT holder gets at least SEARCH so the
# operator console is usable before custom realm roles are assigned.
DEFAULT_AUTHENTICATED_PERMISSIONS: frozenset[Permission] = frozenset({Permission.SEARCH})

# Reviewer: edit / review / approve within state — no upload, no delete.
REVIEWER_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.REVIEW,
        Permission.SEARCH,
    }
)

# Contributor: upload, edit own, delete own, view all, approve own docs.
# Ownership (own docs only) is enforced at the API layer when mutating.
CONTRIBUTOR_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.UPLOAD,
        Permission.REVIEW,
        Permission.PIPELINE,
        Permission.SEARCH,
        Permission.DELETE_OWN,
    }
)

# Platform super admin: full console + settings + user management.
SUPERADMIN_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

# Keycloak / realm role names → permissions.
# Names are matched case-insensitively after strip / alias normalization.
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    # Product roles
    UserRole.SUPER_ADMIN.value: SUPERADMIN_PERMISSIONS,
    UserRole.CONTRIBUTOR.value: CONTRIBUTOR_PERMISSIONS,
    UserRole.REVIEWER.value: REVIEWER_PERMISSIONS,
    # Common aliases / legacy
    "superadmin": SUPERADMIN_PERMISSIONS,
    "super-admin": SUPERADMIN_PERMISSIONS,
    "master_admin": SUPERADMIN_PERMISSIONS,
    "master-admin": SUPERADMIN_PERMISSIONS,
    "realm-admin": SUPERADMIN_PERMISSIONS,
    "content_curator": CONTRIBUTOR_PERMISSIONS,
    "curator": CONTRIBUTOR_PERMISSIONS,
    "operator": CONTRIBUTOR_PERMISSIONS,
    "admin": CONTRIBUTOR_PERMISSIONS,  # state operator, not platform super
    "state_admin": CONTRIBUTOR_PERMISSIONS,
    "state-admin": CONTRIBUTOR_PERMISSIONS,
    "viewer": REVIEWER_PERMISSIONS,
    "user": DEFAULT_AUTHENTICATED_PERMISSIONS,
    "reader": REVIEWER_PERMISSIONS,
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
    - ``contributor`` → upload, review, pipeline, search, delete_own
    - ``reviewer`` → review, search
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
