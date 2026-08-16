"""Authenticated principal extracted from a JWT or local bypass."""

from __future__ import annotations

from dataclasses import dataclass, field

from .groups import ROLE_BH_VIEWER, ROLE_STATE_VIEW, ROLE_SUPER_ADMIN, normalize_state_code
from .permissions import Permission

# Platform superadmin — full permissions AND every instance.
SUPERADMIN_ROLES = frozenset(
    {
        "superadmin",
        "super_admin",
        "super-admin",
    }
)

# Bharat Vistaar viewer — every instance, but only the permissions the role
# actually grants (search). Deliberately NOT a superadmin: keeping these sets
# separate is what stops a read-only global account from gaining write access.
GLOBAL_READ_ROLES = frozenset(
    {
        "bh_viewer",
        "bh-viewer",
        "bh_view",
    }
)

# Roles not limited by the JWT ``instances`` claim.
# State-level roles are restricted to their claimed instances (tenants/states).
INSTANCE_UNRESTRICTED_ROLES = SUPERADMIN_ROLES | GLOBAL_READ_ROLES


@dataclass
class AuthUser:
    user_id: str
    username: str = ""
    email: str = ""
    roles: list[str] = field(default_factory=list)
    permissions: set[Permission] = field(default_factory=set)
    instances: list[str] = field(default_factory=list)
    envs: list[str] = field(default_factory=list)
    # Full Keycloak group paths from the JWT ``groups`` claim.
    groups: list[str] = field(default_factory=list)
    # Per-state role map derived from groups, e.g. {"mh": "contributor", "up": "reviewer"}.
    state_roles: dict[str, str] = field(default_factory=dict)
    token_disabled_mode: bool = False

    def has_permission(self, permission: Permission | str) -> bool:
        needed = permission if isinstance(permission, Permission) else Permission(str(permission))
        return needed in self.permissions

    @property
    def _role_keys(self) -> set[str]:
        return {(role or "").strip().lower() for role in self.roles}

    @property
    def is_superadmin(self) -> bool:
        """True when any role is platform superadmin (all instances + full perms)."""
        return bool(SUPERADMIN_ROLES & self._role_keys)

    @property
    def is_bh_viewer(self) -> bool:
        """True for the Bharat Vistaar global read-only role."""
        return bool(GLOBAL_READ_ROLES & self._role_keys)

    @property
    def is_admin(self) -> bool:
        """Backward-compatible alias for platform superadmin checks.

        Prefer :pyattr:`is_superadmin`. State-level operators are NOT included.
        """
        return self.is_superadmin

    def is_instance_unrestricted(self) -> bool:
        """True when the caller may access every instance (all tenants/states).

        Two cases: local bypass mode with no scoped claim, or platform
        superadmin even when the token carries a narrow ``instances`` claim.
        """
        if self.token_disabled_mode and not self.instances:
            return True
        return bool(INSTANCE_UNRESTRICTED_ROLES & self._role_keys)

    def has_instance(self, instance: str) -> bool:
        if not self.instances:
            # Empty instance list means "no tenant restriction yet" only in disabled mode.
            return self.token_disabled_mode
        return instance.strip().lower() in {i.lower() for i in self.instances}

    def has_env(self, env: str) -> bool:
        if not self.envs:
            return self.token_disabled_mode
        return env.strip().lower() in {e.lower() for e in self.envs}

    def role_for_instance(self, instance: str | None) -> str | None:
        """Role in a given state; super_admin always returns super_admin.

        A ``bh_viewer`` reads every state, so any instance falls back to
        STATE_VIEW when no stronger per-state role is held.
        """
        if self.is_superadmin:
            return ROLE_SUPER_ADMIN
        if not instance:
            return None
        role = self.state_roles.get(normalize_state_code(instance))
        if role:
            return role
        return ROLE_STATE_VIEW if self.is_bh_viewer else None

    def has_permission_for_instance(
        self, permission: Permission | str, instance: str | None
    ) -> bool:
        """Permission check scoped to a state when per-state roles are present.

        Super-admin: global permissions. Users without ``state_roles`` fall
        back to global ``permissions`` (legacy tokens with only realm roles +
        instances claim). With ``state_roles``, the role for that state is
        mapped to permissions.
        """
        needed = permission if isinstance(permission, Permission) else Permission(str(permission))
        if self.is_superadmin or self.token_disabled_mode:
            return needed in self.permissions or needed in set(Permission)

        if not self.state_roles:
            return needed in self.permissions

        role = self.role_for_instance(instance)
        if not role:
            return False
        from .permissions import permissions_for_roles

        return needed in permissions_for_roles([role])


def local_bypass_user() -> AuthUser:
    """Synthetic user when AUTH_DISABLED=true — full access for local/dev continuity."""
    return AuthUser(
        user_id="local-dev",
        username="local-dev",
        email="local-dev@localhost",
        roles=["super_admin"],
        permissions=set(Permission),
        instances=[],  # unrestricted in bypass mode
        envs=["dev", "prod"],
        groups=["/global/super-admin"],
        state_roles={},
        token_disabled_mode=True,
    )
