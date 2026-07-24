"""Authenticated principal extracted from a JWT or local bypass."""

from __future__ import annotations

from dataclasses import dataclass, field

from .permissions import Permission, permissions_for_roles

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
    # Realm-level roles only (from ``realm_access``), distinct from the
    # group/tenant-derived per-tenant roles. Captured for parity with ``main``
    # and future strict platform-admin gating; bh-main's ``is_superadmin`` keeps
    # keying off the wider ``roles`` union (unchanged).
    realm_roles: list[str] = field(default_factory=list)
    permissions: set[Permission] = field(default_factory=set)
    instances: list[str] = field(default_factory=list)
    envs: list[str] = field(default_factory=list)
    # Per-tenant roles: {instance_id: {role, ...}}. Populated from the
    # ``tenant_roles`` / ``groups`` claim UNIONed with the app-side
    # ``tenant_members`` store. EMPTY in legacy flat-claim mode, in which case
    # ``roles`` apply uniformly across the caller's claimed ``instances``.
    tenant_roles: dict[str, set[str]] = field(default_factory=dict)
    token_disabled_mode: bool = False

    def has_permission(self, permission: Permission | str) -> bool:
        """Any-instance view: True if the caller holds ``permission`` anywhere.

        Kept as the compat gate on non-doc-scoped routes. Doc-scoped routes must
        use :meth:`permissions_in` to check the acting tenant.
        """
        needed = permission if isinstance(permission, Permission) else Permission(str(permission))
        return needed in self.permissions

    def roles_in(self, instance: str) -> set[str]:
        """Roles the caller holds *within* ``instance`` (lowercased).

        - Data-unrestricted callers (bh-main platform ``superadmin`` / local
          bypass) hold their roles in every instance.
        - With a ``tenant_roles`` map, only the roles assigned in that instance.
        - Legacy flat-claim mode (no ``tenant_roles``): the caller's flat
          ``roles`` apply in every instance they can access (today's behaviour).
        """
        key = (instance or "").strip().lower()
        if self.is_instance_unrestricted():
            return {(r or "").strip().lower() for r in self.roles if (r or "").strip()}
        if self.tenant_roles:
            return {(r or "").strip().lower() for r in self.tenant_roles.get(key, set())}
        # Legacy flat claims: flat roles apply in every claimed instance.
        if self.has_instance(key):
            return {(r or "").strip().lower() for r in self.roles if (r or "").strip()}
        return set()

    def permissions_in(self, instance: str) -> set[Permission]:
        """Permissions the caller holds within ``instance`` (union of its roles).

        A data-unrestricted caller (bh-main ``superadmin`` / local bypass) holds
        every permission in every instance; otherwise permissions are derived
        from :meth:`roles_in` for that specific tenant.
        """
        if self.is_instance_unrestricted():
            return set(Permission)
        return permissions_for_roles(self.roles_in(instance))

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

    @property
    def is_platform_admin(self) -> bool:
        """Control-plane gate: local bypass OR platform ``superadmin``.

        Gates tenant-registry / lifecycle operations that are NOT scoped to a
        single tenant (create/suspend/delete tenants, provision tenant members,
        reconcile). bh-main flavour: keyed off :pyattr:`is_superadmin` (which is
        also DATA-unrestricted here — bh-main deliberately keeps a
        data-unrestricted superadmin) OR the local-bypass mode. A per-tenant
        ``state_admin`` / ``content_curator`` is NOT a platform admin.
        """
        return self.token_disabled_mode or self.is_superadmin

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
