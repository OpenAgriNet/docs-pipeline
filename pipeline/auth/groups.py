"""Parse Keycloak group paths into tenants (states) and roles.

Expected Keycloak group layout (full paths in the JWT ``groups`` claim):

- ``/global/super-admin``          → platform SUPER_ADMIN (all states)
- ``/states/{STATE_CODE}/{role}``  → role within one state tenant
  e.g. ``/states/MH/contributor``, ``/states/UP/reviewer``

A user may hold different roles in different states. Role names in the path
leaf match :class:`~pipeline.auth.permissions.UserRole` values
(``super_admin``, ``contributor``, ``reviewer``). Hyphenated aliases such as
``super-admin`` are normalized to underscores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# Canonical product roles (JWT / Keycloak group leaves + realm roles).
ROLE_SUPER_ADMIN = "super_admin"
ROLE_CONTRIBUTOR = "contributor"
ROLE_REVIEWER = "reviewer"

CANONICAL_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_CONTRIBUTOR, ROLE_REVIEWER})

# Higher number wins when a user is in multiple groups for the same state.
_ROLE_RANK: dict[str, int] = {
    ROLE_SUPER_ADMIN: 100,
    ROLE_CONTRIBUTOR: 50,
    ROLE_REVIEWER: 10,
}

# Leaf / realm-role aliases → canonical role.
_ROLE_ALIASES: dict[str, str] = {
    "super_admin": ROLE_SUPER_ADMIN,
    "super-admin": ROLE_SUPER_ADMIN,
    "superadmin": ROLE_SUPER_ADMIN,
    "master_admin": ROLE_SUPER_ADMIN,
    "master-admin": ROLE_SUPER_ADMIN,
    "contributor": ROLE_CONTRIBUTOR,
    "reviewer": ROLE_REVIEWER,
    # Legacy realm roles (still accepted if present without groups)
    "content_curator": ROLE_CONTRIBUTOR,
    "curator": ROLE_CONTRIBUTOR,
    "operator": ROLE_CONTRIBUTOR,
    "admin": ROLE_CONTRIBUTOR,
    "state_admin": ROLE_CONTRIBUTOR,
    "state-admin": ROLE_CONTRIBUTOR,
    "viewer": ROLE_REVIEWER,
    "reader": ROLE_REVIEWER,
    "user": ROLE_REVIEWER,
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
            # Also accept /states/{STATE} alone as "member of state" with no role
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0].lower() == "states":
                state = normalize_state_code(parts[1])
                if state:
                    access.state_roles.setdefault(state, ROLE_REVIEWER)
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
