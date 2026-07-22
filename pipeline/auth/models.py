"""Authenticated principal extracted from a JWT or local bypass."""

from __future__ import annotations

from dataclasses import dataclass, field

from .permissions import Permission

# Platform superadmin only — not limited by JWT ``instances`` claim.
# State-level ``admin`` is restricted to their claimed instances (tenants/states).
INSTANCE_UNRESTRICTED_ROLES = frozenset(
    {
        "superadmin",
        "super_admin",
        "master_admin",  # legacy alias
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

        Prefer :pyattr:`is_superadmin`. State-level ``admin`` is NOT included.
        """
        return self.is_superadmin

    def is_instance_unrestricted(self) -> bool:
        """True when the caller may access every instance (all tenants/states).

        Two cases: local bypass mode with no scoped claim, or platform
        superadmin (``superadmin`` / ``master_admin``) even when the token
        carries a narrow ``instances`` claim. State-level ``admin`` is scoped.
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


def local_bypass_user() -> AuthUser:
    """Synthetic user when AUTH_DISABLED=true — full access for local/dev continuity."""
    from .permissions import Permission

    return AuthUser(
        user_id="local-dev",
        username="local-dev",
        email="local-dev@localhost",
        roles=["superadmin"],
        permissions=set(Permission),
        instances=[],  # unrestricted in bypass mode
        envs=["dev", "prod"],
        token_disabled_mode=True,
    )
