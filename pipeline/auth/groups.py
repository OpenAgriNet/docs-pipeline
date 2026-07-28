"""Parse Keycloak group paths into tenants (states) and roles.

Expected Keycloak group layout (full paths in the JWT ``groups`` claim):

- ``/global/super-admin``          → platform SUPER_ADMIN (Bharat Vistaar)
- ``/states/{STATE_CODE}/admin``   → state_admin (full access in that state)
- ``/states/{STATE_CODE}/view``    → state_view (view-only in that state)

Legacy leaves still accepted:

- ``/states/{STATE}/contributor``  → state_admin
- ``/states/{STATE}/reviewer``     → state_view
- ``/states/{STATE}/state-admin``  → state_admin
- ``/states/{STATE}/state-view``   → state_view

A user may hold different roles in different states.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# Canonical product roles (JWT / Keycloak group leaves + realm roles).
ROLE_SUPER_ADMIN = "super_admin"
ROLE_STATE_ADMIN = "state_admin"
ROLE_STATE_VIEW = "state_view"

# Back-compat aliases used by older code / tests.
ROLE_CONTRIBUTOR = ROLE_STATE_ADMIN
ROLE_REVIEWER = ROLE_STATE_VIEW

CANONICAL_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_STATE_ADMIN, ROLE_STATE_VIEW})

# Higher number wins when a user is in multiple groups for the same state.
_ROLE_RANK: dict[str, int] = {
    ROLE_SUPER_ADMIN: 100,
    ROLE_STATE_ADMIN: 50,
    ROLE_STATE_VIEW: 10,
}

# Leaf / realm-role aliases → canonical role.
_ROLE_ALIASES: dict[str, str] = {
    "super_admin": ROLE_SUPER_ADMIN,
    "super-admin": ROLE_SUPER_ADMIN,
    "superadmin": ROLE_SUPER_ADMIN,
    "master_admin": ROLE_SUPER_ADMIN,
    "master-admin": ROLE_SUPER_ADMIN,
    # State admin (full access for that state)
    "state_admin": ROLE_STATE_ADMIN,
    "state-admin": ROLE_STATE_ADMIN,
    "admin": ROLE_STATE_ADMIN,
    "contributor": ROLE_STATE_ADMIN,  # legacy
    "content_curator": ROLE_STATE_ADMIN,
    "curator": ROLE_STATE_ADMIN,
    "operator": ROLE_STATE_ADMIN,
    # State view (view-only for that state)
    "state_view": ROLE_STATE_VIEW,
    "state-view": ROLE_STATE_VIEW,
    "view": ROLE_STATE_VIEW,
    "viewer": ROLE_STATE_VIEW,
    "reviewer": ROLE_STATE_VIEW,  # legacy
    "reader": ROLE_STATE_VIEW,
    "user": ROLE_STATE_VIEW,
}

_STATE_GROUP_RE = re.compile(
    r"^/states/(?P<state>[A-Za-z0-9_-]+)/(?P<role>[A-Za-z0-9_-]+)/?$",
    re.IGNORECASE,
)
_GLOBAL_SUPER_RE = re.compile(
    r"^/global/(?P<role>super[_-]?admin|master[_-]?admin)/?$",
    re.IGNORECASE,
)


def normalize_role(value: str | None) -> str | None:
    """Normalize a role leaf or realm role name to a canonical role, if known."""
    if not value:
        return None
    key = value.strip().lower().replace(" ", "_")
    if not key:
        return None
    if key in _ROLE_ALIASES:
        return _ROLE_ALIASES[key]
    # super_admin-style after hyphen→underscore
    underscored = key.replace("-", "_")
    if underscored in _ROLE_ALIASES:
        return _ROLE_ALIASES[underscored]
    return None


def normalize_state_code(value: str | None) -> str:
    """State/tenant codes are stored lowercase (mh, up, bh)."""
    return (value or "").strip().lower()


@dataclass
class GroupAccess:
    """Access derived from Keycloak ``groups`` claim paths."""

    is_super_admin: bool = False
    # instance (state) → highest role in that state
    state_roles: dict[str, str] = field(default_factory=dict)
    # flat list of group paths as received
    groups: list[str] = field(default_factory=list)
    # union of canonical roles (includes super_admin when global)
    roles: list[str] = field(default_factory=list)

    @property
    def instances(self) -> list[str]:
        """State codes the user may access (empty when super-admin = all)."""
        return sorted(self.state_roles.keys())


def _prefer_role(current: str | None, candidate: str) -> str:
    if current is None:
        return candidate
    return candidate if _ROLE_RANK.get(candidate, 0) > _ROLE_RANK.get(current, 0) else current


def parse_group_paths(groups: Iterable[str] | None) -> GroupAccess:
    """Parse full Keycloak group paths into super-admin flag + per-state roles."""
    access = GroupAccess()
    if not groups:
        return access

    paths: list[str] = []
    for raw in groups:
        if not isinstance(raw, str):
            continue
        path = raw.strip()
        if not path:
            continue
        # Normalize: ensure leading slash, collapse trailing slash (except root)
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/") or "/"
        paths.append(path)

        global_match = _GLOBAL_SUPER_RE.match(path)
        if global_match:
            access.is_super_admin = True
            continue

        state_match = _STATE_GROUP_RE.match(path)
        if not state_match:
            # Also accept /states/{STATE} alone as "member of state" with view
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0].lower() == "states":
                state = normalize_state_code(parts[1])
                if state:
                    access.state_roles.setdefault(state, ROLE_STATE_VIEW)
            continue

        state = normalize_state_code(state_match.group("state"))
        role = normalize_role(state_match.group("role"))
        if not state or not role:
            continue
        if role == ROLE_SUPER_ADMIN:
            # Super-admin under a state still means full platform access.
            access.is_super_admin = True
            continue
        access.state_roles[state] = _prefer_role(access.state_roles.get(state), role)

    access.groups = sorted(set(paths))
    role_set: set[str] = set()
    if access.is_super_admin:
        role_set.add(ROLE_SUPER_ADMIN)
    role_set.update(access.state_roles.values())
    access.roles = sorted(role_set, key=lambda r: (-_ROLE_RANK.get(r, 0), r))
    return access


def extract_groups_claim(claims: dict[str, Any]) -> list[str]:
    """Read multivalued ``groups`` (or ``group``) claim from a JWT payload."""
    for key in ("groups", "group"):
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


def role_for_instance(access: GroupAccess, instance: str | None) -> str | None:
    """Return the user's role for a state instance, or SUPER_ADMIN when global."""
    if access.is_super_admin:
        return ROLE_SUPER_ADMIN
    if not instance:
        return None
    return access.state_roles.get(normalize_state_code(instance))


def group_leaf_for_role(role: str | None) -> str:
    """Preferred Keycloak group leaf name for a product role."""
    canon = normalize_role(role) or (role or "").strip().lower()
    if canon == ROLE_SUPER_ADMIN:
        return "super-admin"
    if canon == ROLE_STATE_ADMIN:
        return "admin"
    if canon == ROLE_STATE_VIEW:
        return "view"
    return canon or "view"
