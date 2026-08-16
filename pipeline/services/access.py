"""Authorization and tenant-scope helpers shared by HTTP routers."""

from collections.abc import Collection
from typing import Optional

from fastapi import HTTPException

from .. import db
from ..auth.deps import assert_permission_in_instance
from ..auth.models import AuthUser
from ..auth.permissions import Permission
from ..auth.tenancy import (
    allowed_instances,
    assert_document_instance_access,
    assert_instance_access,
    default_instance,
    normalize_instance,
    user_can_access_instance,
)


def instance_scope_for_user(user: AuthUser) -> Optional[list[str]]:
    """Return ``None`` for unrestricted callers, otherwise allowed tenants."""
    allowed = allowed_instances(user)
    if allowed is None:
        return None
    return sorted(allowed)


def resolve_create_instance(user: AuthUser, requested: Optional[str] = None) -> str:
    """Resolve the target tenant and require upload permission in that tenant."""
    instance = assert_instance_access(user, requested or default_instance())
    assert_permission_in_instance(user, instance, Permission.UPLOAD)
    return instance


def require_document_for_user(
    workflow_id: str,
    user: AuthUser,
    permission: Optional[Permission] = None,
) -> dict:
    """Load an accessible document and optionally require a tenant permission."""
    document = assert_document_instance_access(user, db.get_document(workflow_id))
    if permission is not None:
        assert_permission_in_instance(user, document.get("instance"), permission)
    return document


def document_for_user_or_none(
    workflow_id: str,
    user: AuthUser,
    permission: Optional[Permission] = None,
) -> Optional[dict]:
    """Return an accessible document, or ``None`` for bulk-operation paths."""
    document = db.get_document(workflow_id)
    if not document or not user_can_access_instance(user, document.get("instance")):
        return None
    if permission is not None and permission not in user.permissions_in(
        document.get("instance") or ""
    ):
        return None
    return document


def assert_index_access(
    user: AuthUser,
    instance: str | None,
    name: Optional[str] = None,
) -> Optional[str]:
    """Validate logical-index ownership and return its physical index name."""
    from . import indexes

    normalized = normalize_instance(instance)
    if not user_can_access_instance(user, normalized):
        raise HTTPException(404, "Index not found")
    physical = db.resolve_marqo_index(normalized, name)
    if physical is None:
        if name:
            raise HTTPException(404, "Index not found")
        if normalized == default_instance():
            physical = indexes.default_physical_index()
        else:
            return None
    return physical


def assert_marqo_index_access(user: AuthUser, marqo_index: str) -> str:
    """Validate access to a caller-supplied physical Marqo index."""
    physical = (marqo_index or "").strip()
    row = db.get_index_by_marqo_index(physical)
    if row is not None:
        if not user_can_access_instance(user, row["instance"]):
            raise HTTPException(404, "Index not found")
        return physical
    if allowed_instances(user) is not None:
        raise HTTPException(404, "Index not found")
    return physical


def assert_tenant_scope(
    user: AuthUser,
    instance: str | None,
    *,
    permission: Permission | Collection[Permission] | None = None,
    platform_admin_allowed: bool = False,
    detail: str,
    platform_denied_detail: str | None = None,
) -> str:
    """Apply the shared tenant 404-hide and wrong-role 403 policy."""
    normalized = normalize_instance(instance)
    known = db.get_tenant(normalized) is not None
    denied_platform = platform_denied_detail or detail

    if platform_admin_allowed and user.is_platform_admin:
        if not known:
            raise HTTPException(404, "Tenant not found")
        return normalized

    if not user_can_access_instance(user, normalized):
        if user.is_platform_admin:
            raise HTTPException(403, denied_platform)
        raise HTTPException(404, "Tenant not found")

    if platform_admin_allowed and not known:
        raise HTTPException(404, "Tenant not found")

    if permission is None:
        return normalized

    needed = (
        frozenset({permission})
        if isinstance(permission, Permission)
        else frozenset(permission)
    )
    if user.permissions_in(normalized).isdisjoint(needed):
        raise HTTPException(403, detail)
    return normalized


def assert_can_manage_indexes(user: AuthUser, instance: str | None) -> str:
    return assert_tenant_scope(
        user,
        instance,
        permission=frozenset({Permission.ADMIN, Permission.PIPELINE}),
        platform_admin_allowed=False,
        detail="Requires admin or pipeline in tenant",
        platform_denied_detail=(
            "Managing a tenant's indexes requires admin or pipeline in that tenant"
        ),
    )


def assert_can_view_tenant(user: AuthUser, instance: str | None) -> str:
    return assert_tenant_scope(
        user,
        instance,
        permission=None,
        platform_admin_allowed=False,
        detail="Viewing a tenant's indexes requires membership in that tenant",
    )


def assert_can_manage_taxonomy(user: AuthUser, instance: str | None) -> str:
    return assert_tenant_scope(
        user,
        instance,
        permission=Permission.ADMIN,
        platform_admin_allowed=True,
        detail="Managing a tenant's taxonomy requires admin in that tenant",
    )


def resolve_taxonomy_read_instance(user: AuthUser, instance: Optional[str]) -> str:
    """Choose the tenant whose taxonomy an authorized caller reads."""
    if instance:
        return assert_instance_access(user, instance)
    allowed = allowed_instances(user)
    if allowed is None:
        return default_instance()
    if not allowed:
        raise HTTPException(403, "No tenant is associated with this account")
    if len(allowed) == 1:
        return next(iter(allowed))
    default = default_instance()
    return default if default in allowed else sorted(allowed)[0]
