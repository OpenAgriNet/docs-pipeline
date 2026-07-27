"""Authenticated principal extracted from a JWT or local bypass."""

from __future__ import annotations

from dataclasses import dataclass, field

from .groups import ROLE_SUPER_ADMIN, normalize_state_code
from .permissions import Permission

# Platform superadmin only — not limited by JWT ``instances`` claim.
# State-level roles are restricted to their claimed instances (tenants/states).
INSTANCE_UNRESTRICTED_ROLES = frozenset(
    {
        "superadmin",
        "super_admin",
        "super-admin",
        "master_admin",  # legacy alias
        "master-admin",
        "realm-admin",
    }
)


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
    def is_superadmin(self) -> bool:
        """True when any role is platform superadmin (all instances + full perms)."""
        return bool(
            INSTANCE_UNRESTRICTED_ROLES
            & {(role or "").strip().lower() for role in self.roles}
        )

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
        return self.is_superadmin

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
        """Role in a given state; super_admin always returns super_admin."""
        if self.is_superadmin:
            return ROLE_SUPER_ADMIN
        if not instance:
            return None
        return self.state_roles.get(normalize_state_code(instance))

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
