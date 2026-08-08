"""Tenant registry, membership, indexes and taxonomy."""

import json
import logging
import re
from fastapi import APIRouter, HTTPException, Query
from ..auth.deps import CurrentUser, RequirePlatformAdmin
from ..auth.permissions import Permission
from ..auth.tenancy import normalize_instance
from ..keycloak_admin import (
    KeycloakAdminError,
    KeycloakAdminForbidden,
    KeycloakAdminUnconfigured,
)
from ..vector_store import is_valid_logical_index_name

router = APIRouter()


@router.get("/tenants/{instance}/indexes")
async def list_tenant_indexes(instance: str, user: CurrentUser):
    """List a tenant's registered indexes. Gated: any access to that tenant."""
    inst = api._assert_can_view_tenant(user, instance)
    return [api._index_row_response(r) for r in api.db.list_indexes(inst)]


@router.post("/tenants/{instance}/indexes")
async def create_tenant_index(instance: str, payload: dict, user: CurrentUser):
    """Provision an additional index within a tenant (self-service).

    Body: ``{name, embedding_model?, settings?}``. Creates the physical Marqo
    index ``<namespace><instance>-<name>`` with the passage schema and
    inserts the registry row (``is_default`` when it is the tenant's first index).
    Gated: caller needs ``admin`` or ``pipeline`` **in** ``{instance}``.
    """
    inst = api._assert_can_manage_indexes(user, instance)
    name = (payload.get("name") or "").strip().lower()
    if not name:
        raise HTTPException(400, "name is required")
    if not is_valid_logical_index_name(name):
        raise HTTPException(400, "name must match ^[a-z0-9_]{1,40}$ (letters, digits, _ only)")
    if api.db.get_index(inst, name):
        raise HTTPException(409, f"Index '{name}' already exists for tenant")

    embedding_model = payload.get("embedding_model") or None
    settings_override = payload.get("settings") if isinstance(payload.get("settings"), dict) else None
    marqo_index = api._new_marqo_index_name(inst, name)
    if api.db.get_index_by_marqo_index(marqo_index):
        raise HTTPException(409, f"Physical index '{marqo_index}' already registered")

    try:
        api._create_marqo_index_with_schema(marqo_index, embedding_model, settings_override)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Failed to create Marqo index: {exc}") from exc

    is_first = not api.db.list_indexes(inst)
    row = api.db.create_index_row(
        instance=inst,
        name=name,
        marqo_index=marqo_index,
        embedding_model=embedding_model,
        settings_json=json.dumps(settings_override) if settings_override else None,
        is_default=is_first,
    )
    return api._index_row_response(row)


@router.delete("/tenants/{instance}/indexes/{name}")
async def delete_tenant_index(
    instance: str,
    name: str,
    user: CurrentUser,
    force: bool = Query(False, description="Drop even when the index still has documents (they are reassigned to the tenant default)"),
):
    """Drop a tenant's index: delete the Marqo index + registry row.

    Gated: ``admin`` in the tenant. Guards:
      * The tenant **default** cannot be dropped unless it is the last index or
        ``force`` is set.
      * If the index still has documents, the call is rejected (409) unless
        ``force`` is set, in which case those documents are reassigned to the
        tenant default (their ``index`` reset to NULL) before the drop.
    """
    inst = api._assert_can_manage_indexes(user, instance)
    if Permission.ADMIN not in user.permissions_in(inst):
        raise HTTPException(403, "Requires admin in tenant")
    row = api.db.get_index(inst, name)
    if not row:
        raise HTTPException(404, "Index not found")

    indexes = api.db.list_indexes(inst)
    is_last = len(indexes) <= 1
    if row.get("is_default") and not (force or is_last):
        raise HTTPException(
            409,
            "Cannot delete the tenant default index. Set another default first, or pass ?force=true.",
        )

    doc_count = api.db.count_documents_for_index(inst, name, include_default_null=bool(row.get("is_default")))
    reassigned = 0
    if doc_count > 0:
        if not force:
            raise HTTPException(
                409,
                f"Index still has {doc_count} document(s). Pass ?force=true to reassign them to the tenant default and drop.",
            )
        reassigned = api.db.reassign_documents_to_default_index(inst, name)

    marqo_dropped = False
    marqo_error = None
    try:
        api.get_vector_store().delete_index(row["marqo_index"])
        marqo_dropped = True
    except Exception as exc:
        marqo_error = str(exc)

    api.db.delete_index_row(inst, name)
    return {
        "instance": inst,
        "name": (name or "").strip().lower(),
        "marqo_index": row["marqo_index"],
        "marqo_dropped": marqo_dropped,
        "marqo_error": marqo_error,
        "documents_reassigned": reassigned,
    }


@router.get("/tenants")
async def list_tenants_route(user: RequirePlatformAdmin):
    """List app-side tenant registry rows (platform super-admin)."""
    return api.db.list_tenants()


@router.post("/tenants/reconcile")
async def reconcile_tenants_route(user: RequirePlatformAdmin):
    """Backfill the tenant registry from documents + indexes + Keycloak orgs.

    Gated: ``master_admin`` / ``RequirePlatformAdmin``. Registry-only and
    non-destructive (no Marqo / Keycloak mutation). Returns
    ``{reconciled: [...], count: N}``.
    """
    reconciled = api.reconcile_tenants(include_keycloak=True)
    return {"reconciled": reconciled, "count": len(reconciled)}


@router.post("/tenants")
async def create_tenant_route(payload: dict, user: RequirePlatformAdmin):
    """Create (or adopt) a tenant: registry row + default index + Keycloak.

    Body: ``{instance, display_name?}``. Gated: ``master_admin`` /
    ``RequirePlatformAdmin``.

    **Idempotent / adopt.** If the requested ``instance`` already exists — a
    ``tenants`` row, an existing ``tenant_indexes`` entry, *or* an existing
    Keycloak Organization — the call *adopts* it instead of erroring or
    duplicating: it ensures the registry row + Keycloak org/groups (idempotent)
    and adopts the tenant's existing default index (no second default Marqo index
    is created), returning the existing tenant + default index with
    ``adopted: true``. This makes "Create tenant -> acme" in the UI safe.

    For a genuinely new tenant, the app-side (data-plane) tenant + default Marqo
    index are provisioned, then the identity-plane objects (Keycloak Organization
    + ``/<instance>`` group tree with its ``{admin, content_curator, viewer}``
    role children) are created and the response carries ``adopted: false``.

    **Graceful degradation:** if Keycloak admin is not configured (no
    ``KEYCLOAK_ADMIN_CLIENT_SECRET``) — or a KC call fails — the app-side tenant
    is still created/adopted and returned with a ``warning`` field describing what
    was skipped. Tenant creation never hard-fails on the identity plane.
    """
    instance = (payload.get("instance") or "").strip().lower()
    if not instance:
        raise HTTPException(400, "instance is required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", instance):
        raise HTTPException(400, "instance must be lowercase alphanumeric with - or _")
    display_name = payload.get("display_name")

    # Adopt any instance that already exists de-facto (registry row / index /
    # Keycloak org) rather than 409-ing or creating a duplicate default index.
    existing_default = api.db.get_default_index(instance)
    already_exists = bool(
        api.db.get_tenant(instance)
        or api.db.list_indexes(instance)
        or api._instance_has_kc_org(instance)
    )
    if already_exists:
        return api._adopt_existing_tenant(instance, display_name, existing_default)

    default_marqo_index = api._new_marqo_index_name(instance, "default")
    # Same collision guard as create_tenant_index: never register/adopt a physical
    # index that already belongs to another (instance, name) registry row. Checked
    # BEFORE the tenant row is written so a collision leaves no orphan tenant.
    if api.db.get_index_by_marqo_index(default_marqo_index):
        raise HTTPException(409, f"Physical index '{default_marqo_index}' already registered")

    api.db.create_tenant_row(instance, display_name=display_name)
    try:
        api._create_marqo_index_with_schema(default_marqo_index)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Failed to create default Marqo index: {exc}") from exc
    row = api.db.create_index_row(
        instance=instance,
        name="default",
        marqo_index=default_marqo_index,
        is_default=True,
    )

    keycloak_result, warning = api._provision_tenant_identity(instance, display_name)
    response = {
        "tenant": api.db.get_tenant(instance),
        "default_index": api._index_row_response(row),
        "keycloak": keycloak_result,
        "adopted": False,
    }
    if warning:
        response["warning"] = warning
    return response


@router.post("/tenants/{instance}/members")
async def create_tenant_member_route(instance: str, payload: dict, user: CurrentUser):
    """Create a Keycloak user and add it to a tenant role group.

    Body: ``{username, email?, role}`` where ``role`` ∈
    ``{admin, content_curator, viewer}``. Gated: the caller must be a
    ``master_admin`` (platform admin) or hold ``manage_users`` (i.e. be an
    ``admin``) **in** ``{instance}`` — see :func:`_assert_can_manage_members`.
    Platform roles (``master_admin`` / ``superadmin``) can never be assigned as a
    tenant membership: ``role`` is restricted to the per-tenant ``ROLES``.

    For a **new** user, generates a strong temporary password (the user must change
    it on first login) and returns
    ``{username, role, temporary_password, user_id, created: True}``.

    Adding an **existing** realm account to the tenant is a platform-admin-only
    operation (``allow_existing``): username lookup is realm-wide, so for a tenant
    admin the merge branch would be an account-takeover primitive — name any user,
    join it to the tenant group tree (which grants it this tenant's data), then
    reset its password through the member routes. A tenant admin therefore gets 403
    on an already-taken username and must pick a new one. For a platform admin the
    existing user is only added to the tenant role group — no password is
    set/returned — and the response is
    ``{username, role, user_id, created: False, added_to_group}``.
    404 if ``{instance}`` is not a known/accessible tenant; 403 for a member with
    an insufficient role or a protected/foreign target account; 503 if Keycloak
    admin is unconfigured.
    """
    inst = api._assert_can_manage_members(user, instance)
    username = (payload.get("username") or "").strip()
    if not username:
        raise HTTPException(400, "username is required")
    role = (payload.get("role") or "").strip().lower()
    if role not in api.keycloak_admin.ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(api.keycloak_admin.ROLES)}")
    email = (payload.get("email") or "").strip() or None

    temporary_password = api.keycloak_admin.generate_temporary_password()
    try:
        result = api.keycloak_admin.create_user(
            username=username,
            email=email,
            temporary_password=temporary_password,
            group_path=f"/{inst}/{role}",
            allow_existing=user.is_platform_admin,
        )
    except KeycloakAdminUnconfigured as exc:
        raise api._kc_unconfigured_503(exc) from exc
    except KeycloakAdminForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeycloakAdminError as exc:
        raise api._kc_member_error_502(exc, "user provisioning") from exc

    created = bool(result.get("created", False))
    response = {
        "username": result["username"],
        "role": role,
        "user_id": result.get("id"),
        "created": created,
    }
    if created:
        # Only a newly-created user gets a temporary password (echoed once).
        response["temporary_password"] = result.get("temporary_password", temporary_password)
    else:
        # Existing user: merged into the role group; password left untouched.
        response["added_to_group"] = f"/{inst}/{role}"
    return response


@router.post("/tenants/{instance}/admins")
async def create_tenant_admin_route(instance: str, payload: dict, user: CurrentUser):
    """Create a tenant **admin** user (member of ``/<instance>/admin``).

    Body: ``{username, email?}``. Gated (via the delegated member route): platform
    admin, or ``manage_users`` in ``{instance}``. Convenience form of
    ``POST /tenants/{instance}/members`` with ``role=admin``.

    For a new user returns ``{username, temporary_password, user_id, created: True}``.
    When the username already exists the request is refused with 403 for a tenant
    admin (realm-wide takeover risk — see :func:`create_tenant_member_route`); for a
    platform admin the account is only added to ``/<instance>/admin`` and the
    response is ``{username, user_id, created: False, added_to_group}`` with **no**
    ``temporary_password``.
    404 if ``{instance}`` is not a known tenant; 503 if Keycloak admin is
    unconfigured.
    """
    body = dict(payload or {})
    body["role"] = "admin"
    result = await api.create_tenant_member_route(instance, body, user)
    response = {
        "username": result["username"],
        "user_id": result.get("user_id"),
        "created": result.get("created", False),
    }
    if "temporary_password" in result:
        response["temporary_password"] = result["temporary_password"]
    if "added_to_group" in result:
        response["added_to_group"] = result["added_to_group"]
    return response


@router.get("/tenants/{instance}/members")
async def list_tenant_members_route(instance: str, user: CurrentUser):
    """List Keycloak users in any ``/<instance>/*`` role group.

    Gated: platform admin, or ``manage_users`` in ``{instance}`` (see
    :func:`_assert_can_manage_members`). Returns
    ``[{user_id, username, email, roles}]``.
    404 if ``{instance}`` is not a known/accessible tenant; 403 for a member with
    an insufficient role; 503 if Keycloak admin is unconfigured.
    """
    inst = api._assert_can_manage_members(user, instance)
    try:
        return api.keycloak_admin.list_members(inst)
    except KeycloakAdminUnconfigured as exc:
        raise api._kc_unconfigured_503(exc) from exc
    except KeycloakAdminError as exc:
        raise api._kc_member_error_502(exc, "member listing") from exc


@router.delete("/tenants/{instance}/members/{user_id}")
async def remove_tenant_member_route(instance: str, user_id: str, user: CurrentUser):
    """Remove a member from **all** of a tenant's role groups.

    Gated: platform admin, or ``manage_users`` in ``{instance}``. Detaches
    ``user_id`` from every ``/<instance>/*`` role group (it remains a Keycloak
    account, just no longer a member of this tenant). Returns
    ``{instance, user_id, removed_roles, failed_roles, removed}``; ``removed`` is
    ``False`` (with the role names in ``failed_roles``) when some group detaches
    failed, so a half-removed member is never reported as a clean success.
    404 if the tenant is unknown/inaccessible **or** ``user_id`` is not a member
    of it (no cross-tenant leak); 403 for an insufficient role, self-removal or a
    protected/foreign target; 409 when it is the tenant's only admin; 503 if
    Keycloak admin is unconfigured.
    """
    inst = api._assert_can_manage_members(user, instance)
    api._assert_not_self(user, user_id, "remove")
    api._assert_not_last_admin(user, inst, user_id, "remove")
    try:
        result = api.keycloak_admin.remove_from_group(
            inst, user_id, platform_admin=user.is_platform_admin
        )
    except KeycloakAdminUnconfigured as exc:
        raise api._kc_unconfigured_503(exc) from exc
    except KeycloakAdminForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeycloakAdminError as exc:
        raise api._kc_member_error_502(exc, "member removal") from exc
    if not result.get("was_member"):
        raise HTTPException(404, "Member not found in tenant")
    failed = result.get("failed_roles") or []
    for entry in failed:
        logging.error(
            "Keycloak member removal: tenant=%s user=%s role=%s failed: %s",
            inst, user_id, entry.get("role"), entry.get("error"),
        )
    return {
        "instance": inst,
        "user_id": user_id,
        "removed_roles": result.get("removed_roles", []),
        # Role names only — the Keycloak detail stays in the log.
        "failed_roles": [entry.get("role") for entry in failed],
        "removed": not failed,
    }


@router.patch("/tenants/{instance}/members/{user_id}")
async def change_tenant_member_role_route(
    instance: str, user_id: str, payload: dict, user: CurrentUser
):
    """Change an existing member's role within a tenant.

    Body: ``{role}`` where ``role`` ∈ ``{admin, content_curator, viewer}``.
    Gated: platform admin, or ``manage_users`` in ``{instance}``. Platform roles
    (``master_admin`` / ``superadmin``) can never be assigned — ``role`` is
    restricted to the per-tenant ``ROLES``. The member is left holding exactly the
    requested role (other tenant role groups are dropped). Returns
    ``{instance, user_id, role, previous_roles}``.
    404 if the tenant is unknown/inaccessible **or** ``user_id`` is not a member
    of it; 400 for a bad role; 403 for an insufficient role, a self-demotion or a
    protected/foreign target; 409 when it would demote the tenant's only admin;
    503 if Keycloak admin is unconfigured.
    """
    inst = api._assert_can_manage_members(user, instance)
    role = (payload.get("role") or "").strip().lower()
    if role not in api.keycloak_admin.ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(api.keycloak_admin.ROLES)}")
    api._assert_not_self(user, user_id, "change the role of")
    if role != "admin":
        api._assert_not_last_admin(user, inst, user_id, "demote")
    try:
        result = api.keycloak_admin.set_member_role(
            inst, user_id, role, platform_admin=user.is_platform_admin
        )
    except KeycloakAdminUnconfigured as exc:
        raise api._kc_unconfigured_503(exc) from exc
    except KeycloakAdminForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeycloakAdminError as exc:
        raise api._kc_member_error_502(exc, "role change") from exc
    if not result.get("was_member"):
        raise HTTPException(404, "Member not found in tenant")
    return {
        "instance": inst,
        "user_id": user_id,
        "role": result.get("role", role),
        "previous_roles": result.get("previous_roles", []),
    }


@router.post("/tenants/{instance}/members/{user_id}/reset-password")
async def reset_tenant_member_password_route(instance: str, user_id: str, user: CurrentUser):
    """Reset a tenant member's password to a fresh temporary one.

    Gated: platform admin, or ``manage_users`` in ``{instance}``. The new password
    is temporary (must-change on first login) and echoed **once** as
    ``temporary_password``. Returns ``{instance, user_id, temporary_password}``.
    404 if the tenant is unknown/inaccessible **or** ``user_id`` is not a member
    of it; 403 for an insufficient role or a protected/foreign target account; 503
    if Keycloak admin is unconfigured.
    """
    inst = api._assert_can_manage_members(user, instance)
    try:
        result = api.keycloak_admin.reset_password(
            inst, user_id, platform_admin=user.is_platform_admin
        )
    except KeycloakAdminUnconfigured as exc:
        raise api._kc_unconfigured_503(exc) from exc
    except KeycloakAdminForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeycloakAdminError as exc:
        raise api._kc_member_error_502(exc, "password reset") from exc
    if not result.get("was_member"):
        raise HTTPException(404, "Member not found in tenant")
    return {
        "instance": inst,
        "user_id": user_id,
        "temporary_password": result.get("temporary_password"),
    }


@router.get("/tenants/{instance}/taxonomy")
async def get_tenant_taxonomy_route(instance: str, user: CurrentUser):
    """Return a tenant's editable tag taxonomy (``{instance, domains: {...}}``).

    Gated exactly like member management (:func:`_assert_can_manage_members`):
    platform admin, or ``manage_users`` (i.e. ``admin``) **in** ``{instance}``.
    The tenant is seeded from the shipped default on FIRST access only, so the
    console shows a populated taxonomy to start with but an admin who empties it
    keeps it empty. 404 for an unknown/cross-tenant instance; 403 for a member
    with an insufficient role.
    """
    inst = api._assert_can_manage_members(user, instance)
    api._ensure_tenant_taxonomy_seeded(inst)
    return api._tenant_taxonomy_payload(inst)


@router.post("/tenants/{instance}/taxonomy/nodes")
async def create_tenant_taxonomy_node_route(instance: str, payload: dict, user: CurrentUser):
    """Add a taxonomy node to a tenant.

    Body: ``{domain, dimension, value?}``. ``value`` omitted/empty registers an
    empty-dimension placeholder (an editable dimension with no vocabulary yet).
    ``domain``/``dimension`` are normalized to lowercase (structural keys);
    ``value`` keeps its casing (the tagging path lowercases at match time).
    Gated: platform admin or ``admin`` in ``{instance}``. 409 if the node already
    exists; 404/403 per the member-management discipline.
    """
    inst = api._assert_can_manage_members(user, instance)
    api._ensure_tenant_taxonomy_seeded(inst)
    domain = (payload.get("domain") or "").strip()
    dimension = (payload.get("dimension") or "").strip()
    value = (payload.get("value") or "").strip()
    if not domain or not dimension:
        raise HTTPException(400, "domain and dimension are required")
    row = api.db.add_taxonomy_node(inst, domain, dimension, value)
    if row is None:
        raise HTTPException(409, "Taxonomy node already exists")
    return row


@router.patch("/tenants/{instance}/taxonomy/nodes")
async def rename_tenant_taxonomy_node_route(instance: str, payload: dict, user: CurrentUser):
    """Rename a taxonomy node's value within its ``domain.dimension``.

    Body: ``{domain, dimension, value, new_value}``. Gated: platform admin or
    ``admin`` in ``{instance}``. 404 if the source node does not exist; 409 if the
    target value already exists for that ``domain.dimension``.
    """
    inst = api._assert_can_manage_members(user, instance)
    api._ensure_tenant_taxonomy_seeded(inst)
    domain = (payload.get("domain") or "").strip()
    dimension = (payload.get("dimension") or "").strip()
    value = (payload.get("value") or "").strip()
    new_value = (payload.get("new_value") or "").strip()
    if not domain or not dimension or not new_value:
        raise HTTPException(400, "domain, dimension and new_value are required")
    try:
        row = api.db.rename_taxonomy_node(inst, domain, dimension, value, new_value)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if row is None:
        raise HTTPException(404, "Taxonomy node not found")
    return row


@router.delete("/tenants/{instance}/taxonomy/nodes")
async def delete_tenant_taxonomy_node_route(
    instance: str,
    user: CurrentUser,
    domain: str = Query(..., description="Taxonomy node domain"),
    dimension: str = Query(..., description="Taxonomy node dimension"),
    value: str = Query(..., min_length=1, description="Taxonomy node value to delete"),
):
    """Delete one taxonomy VALUE from a tenant.

    ``value`` is required (a caller that forgot it used to silently delete the
    empty-dimension placeholder instead); deleting the dimension itself is the
    separate ``/taxonomy/dimensions`` route. Removing a dimension's last value
    keeps the (now empty) dimension. Gated: platform admin or ``admin`` in
    ``{instance}``. 404 if the node does not exist for the tenant.
    """
    inst = api._assert_can_manage_members(user, instance)
    api._ensure_tenant_taxonomy_seeded(inst)
    if not (value or "").strip():
        raise HTTPException(400, "value is required")
    if not api.db.delete_taxonomy_node(inst, domain, dimension, value):
        raise HTTPException(404, "Taxonomy node not found")
    return {
        "instance": inst,
        "domain": (domain or "").strip().lower(),
        "dimension": (dimension or "").strip().lower(),
        "value": (value or "").strip(),
        "deleted": True,
    }


@router.delete("/tenants/{instance}/taxonomy/dimensions")
async def delete_tenant_taxonomy_dimension_route(
    instance: str,
    user: CurrentUser,
    domain: str = Query(..., description="Taxonomy domain"),
    dimension: str = Query(..., description="Taxonomy dimension to remove entirely"),
):
    """Remove a whole ``domain.dimension`` (its values and its placeholder).

    The node delete path deliberately preserves an emptied dimension, so this is
    the only way to retire one. Gated: platform admin or ``admin`` in
    ``{instance}``. 404 when the tenant has no such dimension.
    """
    inst = api._assert_can_manage_members(user, instance)
    api._ensure_tenant_taxonomy_seeded(inst)
    removed = api.db.delete_taxonomy_dimension(inst, domain, dimension)
    if not removed:
        raise HTTPException(404, "Taxonomy dimension not found")
    return {
        "instance": inst,
        "domain": (domain or "").strip().lower(),
        "dimension": (dimension or "").strip().lower(),
        "removed": removed,
        "deleted": True,
    }


@router.post("/tenants/{instance}/suspend")
async def suspend_tenant_route(instance: str, user: RequirePlatformAdmin):
    """Suspend a tenant (data retained). Gated: ``master_admin``."""
    inst = normalize_instance(instance)
    if not api.db.get_tenant(inst):
        raise HTTPException(404, "Tenant not found")
    # TODO(keycloak-org): disable the Keycloak Organization so members can no
    # longer obtain tokens. Handled by the provisioning script / KC Admin API.
    return api.db.set_tenant_status(inst, "suspended")


@router.delete("/tenants/{instance}")
async def delete_tenant_route(
    instance: str,
    user: RequirePlatformAdmin,
    confirm: bool = Query(False, description="Required guard for this destructive delete"),
):
    """Delete a tenant: drop **all** its Marqo indexes + registry rows.

    Gated: ``master_admin``. The destructive drop is guarded behind ``?confirm=true``.
    """
    inst = normalize_instance(instance)
    if not api.db.get_tenant(inst):
        raise HTTPException(404, "Tenant not found")
    if not confirm:
        raise HTTPException(400, "Destructive delete requires ?confirm=true")

    # TODO(keycloak-org): remove the Keycloak Organization (members + roles) via
    # the KC Admin API. Out of scope here; the provisioning script owns KC orgs.
    dropped = []
    for row in api.db.list_indexes(inst):
        error = None
        try:
            api.get_vector_store().delete_index(row["marqo_index"])
        except Exception as exc:
            error = str(exc)
        dropped.append({"marqo_index": row["marqo_index"], "error": error})
    removed = api.db.delete_tenant(inst)
    return {
        "instance": inst,
        "indexes_dropped": dropped,
        "registry_rows_removed": removed,
    }


# Imported last: `pipeline.api` re-exports the handlers above, so a top-level
# import here would be circular. Handlers resolve `api.<name>` at call time,
# which is what keeps `monkeypatch.setattr(api, ...)` biting.
from .. import api  # noqa: E402
