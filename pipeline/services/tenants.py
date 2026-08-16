"""Tenant registry, identity provisioning, and membership helpers."""

import logging
from typing import Optional

from fastapi import HTTPException

from .. import db, keycloak_admin
from ..auth.models import AuthUser
from ..auth.permissions import Permission
from ..keycloak_admin import KeycloakAdminError, KeycloakAdminUnconfigured
from . import access, indexes


def reconcile_tenants(include_keycloak: bool = True) -> list[dict]:
    """Backfill tenant registry rows from local state and Keycloak orgs."""
    instances: dict[str, Optional[str]] = {}
    for inst in db.list_known_instances():
        instances.setdefault(inst, None)

    if include_keycloak:
        try:
            for org in keycloak_admin.list_organizations():
                inst = (org.get("instance") or org.get("name") or "").strip().lower()
                if not inst:
                    continue
                instances[inst] = org.get("name") or instances.get(inst)
        except (KeycloakAdminError, KeycloakAdminUnconfigured) as exc:
            logging.debug("reconcile_tenants: skipping Keycloak orgs: %s", exc)
        except Exception as exc:  # noqa: BLE001 - identity plane must never block reconcile
            logging.debug("reconcile_tenants: Keycloak org lookup failed: %s", exc)

    for inst, display_name in instances.items():
        if not db.get_tenant(inst):
            db.create_tenant_row(inst, display_name=display_name or inst)

    return db.list_tenants()


def instance_has_kc_org(instance: str) -> bool:
    """Best-effort check for a Keycloak Organization for ``instance``."""
    inst = (instance or "").strip().lower()
    try:
        for org in keycloak_admin.list_organizations():
            candidates = {
                (org.get("instance") or "").strip().lower(),
                (org.get("name") or "").strip().lower(),
                (org.get("alias") or "").strip().lower(),
            }
            if inst in candidates:
                return True
    except (KeycloakAdminError, KeycloakAdminUnconfigured):
        return False
    except Exception:  # noqa: BLE001 - identity plane is optional here
        return False
    return False


def provision_tenant_identity(
    instance: str, display_name: Optional[str]
) -> tuple[Optional[dict], Optional[str]]:
    """Best-effort Keycloak Organization and group-tree provisioning."""
    try:
        org_id = keycloak_admin.ensure_organization(instance, display_name=display_name)
        groups = keycloak_admin.ensure_group_tree(instance)
        return {"organization_id": org_id, "groups": sorted(groups.keys())}, None
    except KeycloakAdminUnconfigured as exc:
        return None, "Tenant created without Keycloak provisioning: " + str(exc)
    except KeycloakAdminError as exc:
        return None, f"Tenant created but Keycloak provisioning failed: {exc}"


def adopt_existing_tenant(
    instance: str, display_name: Optional[str], existing_default: Optional[dict]
) -> dict:
    """Adopt a de-facto tenant into the registry without duplicating its index."""
    db.create_tenant_row(instance, display_name=display_name or instance)

    default_row = existing_default or db.get_default_index(instance)
    if default_row is None:
        default_marqo_index = indexes.new_marqo_index_name(instance, "default")
        if db.get_index_by_marqo_index(default_marqo_index):
            raise HTTPException(409, f"Physical index '{default_marqo_index}' already registered")
        try:
            indexes.create_marqo_index_with_schema(default_marqo_index)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"Failed to create default Marqo index: {exc}") from exc
        default_row = db.create_index_row(
            instance=instance,
            name="default",
            marqo_index=default_marqo_index,
            is_default=True,
        )

    keycloak_result, warning = provision_tenant_identity(instance, display_name)
    response = {
        "tenant": db.get_tenant(instance),
        "default_index": index_row_response(default_row) if default_row else None,
        "keycloak": keycloak_result,
        "adopted": True,
    }
    if warning:
        response["warning"] = warning
    return response


def kc_unconfigured_503(exc: KeycloakAdminUnconfigured) -> HTTPException:
    """Translate an inert Keycloak admin into the public 503 contract."""
    return HTTPException(
        503,
        "Keycloak admin is not configured on this server, so tenant user "
        "management is unavailable. Set KEYCLOAK_ADMIN_CLIENT_SECRET (and "
        "KEYCLOAK_ADMIN_CLIENT_ID / KEYCLOAK_ADMIN_BASE_URL / KEYCLOAK_REALM) "
        "to enable it.",
    )


def assert_can_manage_members(user: AuthUser, instance: str) -> str:
    """Require tenant ``manage_users`` or platform-admin authority."""
    return access.assert_tenant_scope(
        user,
        instance,
        permission=Permission.MANAGE_USERS,
        platform_admin_allowed=True,
        detail="Managing a tenant's members requires admin in that tenant",
    )


def kc_member_error_502(exc: KeycloakAdminError, operation: str) -> HTTPException:
    """Log the Keycloak detail and return the generic public 502 contract."""
    logging.error("Keycloak %s failed: %s", operation, exc)
    return HTTPException(502, f"Keycloak {operation} failed. See server logs for details.")


def assert_not_self(user: AuthUser, user_id: str, action: str) -> None:
    """Refuse a tenant membership mutation aimed at the caller."""
    if user_id and user.user_id and user_id == user.user_id:
        raise HTTPException(403, f"You cannot {action} your own tenant membership")


def assert_not_last_admin(user: AuthUser, instance: str, user_id: str, action: str) -> None:
    """Refuse a mutation that would leave a tenant without an admin."""
    if user.is_platform_admin:
        return
    try:
        members = keycloak_admin.list_members(instance)
    except KeycloakAdminUnconfigured as exc:
        raise kc_unconfigured_503(exc) from exc
    except KeycloakAdminError as exc:
        raise kc_member_error_502(exc, "member listing") from exc
    admins = [member for member in members if "admin" in (member.get("roles") or [])]
    if len(admins) == 1 and admins[0].get("user_id") == user_id:
        raise HTTPException(
            409,
            f"Cannot {action} the tenant's only admin — promote another member to "
            "admin first",
        )


def index_row_response(row: dict) -> dict:
    """Shape a tenant index registry row for API responses."""
    return {
        "instance": row.get("instance"),
        "name": row.get("name"),
        "marqo_index": row.get("marqo_index"),
        "embedding_model": row.get("embedding_model"),
        "is_default": bool(row.get("is_default")),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
    }
