"""Named API permissions and role → permission mapping.

Product roles (Keycloak groups / realm roles):

Global (Bharat Vistaar platform level):

- ``super_admin``       — all states, full access incl. settings / users / PROD
- ``bh_viewer``         — all states, view-only

Per state / centre (a centre is just another tenant code):

- ``state_admin``       — everything in the state
- ``state_approver``    — everything in the state except delete
- ``state_contributor`` — everything in the state except delete and DEV publish
                          (may still approve OCR / translation / chunking)
- ``state_view``        — view only

Note ``Permission.REVIEW`` covers *both* "edit documents" and "review &
approve": no product role holds one without the other, so they are not split.
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
    # Approve DEV ingestion ("Approve publish to dev") — the gate that pushes a
    # document into the DEV search index. Split out from REVIEW so a contributor
    # can approve OCR / translation / chunking but not publish.
    APPROVE_INGESTION = "approve_ingestion"
    # State-admin destructive actions (enforced with ownership / tenancy checks).
    DELETE_OWN = "delete_own"


class UserRole(str, Enum):
    """Canonical product roles (mirror Keycloak group leaves / realm roles)."""

    SUPER_ADMIN = "super_admin"
    BH_VIEWER = "bh_viewer"
    STATE_ADMIN = "state_admin"
    STATE_APPROVER = "state_approver"
    STATE_CONTRIBUTOR = "state_contributor"
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

# State contributor: full state access — upload, edit, run pipeline, and approve
# the OCR / translation / chunking review gates — EXCEPT publishing to DEV and
# deleting. The one approval they cannot give is ``approve_ingestion``.
STATE_CONTRIBUTOR_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.SEARCH,
        Permission.UPLOAD,
        Permission.PIPELINE,
        Permission.REVIEW,
    }
)

# State approver: contributor + approve DEV ingestion. Still no delete.
STATE_APPROVER_PERMISSIONS: frozenset[Permission] = STATE_CONTRIBUTOR_PERMISSIONS | {
    Permission.APPROVE_INGESTION,
}

# State admin: approver + delete own documents.
# Prod promote / user management stay platform-only (super_admin).
STATE_ADMIN_PERMISSIONS: frozenset[Permission] = STATE_APPROVER_PERMISSIONS | {
    Permission.DELETE_OWN,
}

# Bharat Vistaar viewer: read-only, but across every state (see models.py for scope).
BH_VIEWER_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.SEARCH,
    }
)

# Platform super admin (Bharat Vistaar): full console + settings + users + prod.
SUPERADMIN_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

# Keycloak / realm role names → permissions.
# Names are matched case-insensitively after strip / alias normalization.
ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    # Product roles
    UserRole.SUPER_ADMIN.value: SUPERADMIN_PERMISSIONS,
    UserRole.BH_VIEWER.value: BH_VIEWER_PERMISSIONS,
    UserRole.STATE_ADMIN.value: STATE_ADMIN_PERMISSIONS,
    UserRole.STATE_APPROVER.value: STATE_APPROVER_PERMISSIONS,
    UserRole.STATE_CONTRIBUTOR.value: STATE_CONTRIBUTOR_PERMISSIONS,
    UserRole.STATE_VIEW.value: STATE_VIEW_PERMISSIONS,
    # Super-admin spellings. ``super-admin`` is the pre-consolidation realm role
    # and stays mapped until every holder is in /global/super-admin.
    "superadmin": SUPERADMIN_PERMISSIONS,
    "super-admin": SUPERADMIN_PERMISSIONS,
    # Bharat Vistaar viewer spellings
    "bh-viewer": BH_VIEWER_PERMISSIONS,
    "bh_view": BH_VIEWER_PERMISSIONS,
    # Group-leaf spellings for the state roles
    "admin": STATE_ADMIN_PERMISSIONS,
    "state-admin": STATE_ADMIN_PERMISSIONS,
    "approver": STATE_APPROVER_PERMISSIONS,
    "state-approver": STATE_APPROVER_PERMISSIONS,
    "contributor": STATE_CONTRIBUTOR_PERMISSIONS,
    "state-contributor": STATE_CONTRIBUTOR_PERMISSIONS,
    "view": STATE_VIEW_PERMISSIONS,
    "state-view": STATE_VIEW_PERMISSIONS,
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
    - ``bh_viewer`` → search only (across all states — see models.py)
    - ``state_admin`` → search, upload, pipeline, review, approve_ingestion, delete_own
    - ``state_approver`` → search, upload, pipeline, review, approve_ingestion
    - ``state_contributor`` → search, upload, pipeline, review (no DEV publish)
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
