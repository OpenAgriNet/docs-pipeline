"""Authenticated principal extracted from a JWT or local bypass."""

from __future__ import annotations

from dataclasses import dataclass, field

from .permissions import Permission, permissions_for_roles

# Realm-level roles that grant platform-wide (all-tenant) access. ONLY the
# platform super-admin. A per-tenant ``admin`` role (assigned within one tenant
# via a group / org membership) grants full permissions *inside that tenant* but
# is NOT platform-unrestricted — otherwise a tenant admin could see every tenant.
INSTANCE_UNRESTRICTED_ROLES = frozenset({"master_admin"})


@dataclass
class AuthUser:
    user_id: str
    username: str = ""
    email: str = ""
    roles: list[str] = field(default_factory=list)
    # Realm-level roles only (from realm_access/resource_access/`roles` claim),
    # distinct from the group-derived per-tenant roles. Drives the
    # instance-unrestricted check so a per-tenant admin is never platform-wide.
    realm_roles: list[str] = field(default_factory=list)
    permissions: set[Permission] = field(default_factory=set)
    instances: list[str] = field(default_factory=list)
    envs: list[str] = field(default_factory=list)
    # Per-tenant roles: {instance_id: {role, ...}}. Populated from the
    # ``tenant_roles`` / ``groups`` claim. Empty in legacy flat-claim mode, in
    # which case ``roles`` apply uniformly across the caller's ``instances``.
    tenant_roles: dict[str, set[str]] = field(default_factory=dict)
    token_disabled_mode: bool = False

    def has_permission(self, permission: Permission | str) -> bool:
        """Any-instance view: True if the caller holds ``permission`` anywhere.

        Kept for compatibility as the gate on non-doc-scoped routes. Doc-scoped
        routes must use :meth:`permissions_in` to check the acting tenant.
        """
        needed = permission if isinstance(permission, Permission) else Permission(str(permission))
        return needed in self.permissions

    def roles_in(self, instance: str) -> set[str]:
        """Roles the caller holds *within* ``instance`` (lowercased).

        - Unrestricted callers (``master_admin`` / ``admin`` / local bypass)
          hold their roles in every instance.
        - With a ``tenant_roles`` map, only the roles assigned in that instance.
        - Legacy flat-claim mode: the caller's flat ``roles`` apply in every
          instance they can access (today's behaviour).
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
        """Permissions the caller holds within ``instance`` (union of its roles)."""
        return permissions_for_roles(self.roles_in(instance))

    @property
    def is_admin(self) -> bool:
        """True when a REALM-level role grants platform-wide (all-tenant) access.

        Checks ``realm_roles`` only — a per-tenant ``admin`` (in ``tenant_roles``)
        must NOT make the caller instance-unrestricted.
        """
        return bool(
            INSTANCE_UNRESTRICTED_ROLES
            & {(role or "").strip().lower() for role in self.realm_roles}
        )

    def is_instance_unrestricted(self) -> bool:
        """True when the caller may access every instance (all tenants).

        Two cases: local bypass mode with no scoped claim, or any admin role
        (``master_admin`` / ``admin``) even when the token carries a narrow
        ``instances`` claim.
        """
        if self.token_disabled_mode and not self.instances:
            return True
        return self.is_admin

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
        roles=["master_admin"],
        realm_roles=["master_admin"],
        permissions=set(Permission),
        instances=[],  # unrestricted in bypass mode
        envs=["dev", "prod"],
        token_disabled_mode=True,
    )
