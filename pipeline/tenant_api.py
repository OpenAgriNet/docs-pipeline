"""Multi-tenancy control-plane routes (tenant / index / member provisioning).

Grafted from ``main``'s ``pipeline/api.py`` tenant routes onto bh-main. This file
is deliberately self-contained: ``api.py`` mounts it with

    from pipeline.tenant_api import router as tenant_router
    app.include_router(tenant_router)

Every route is *additive* — a new control-plane surface. Nothing here changes the
behaviour of the existing document / search routes.

Design notes (bh-main flavour):

* Physical (Qdrant) collection names are NEVER computed here — the registry owns
  that behind :func:`db.ensure_tenant_default_index`. We only pass logical
  ``(instance, name)`` identities and, optionally, a caller-supplied physical
  name (with a cross-tenant collision guard).
* Two authorization planes:
    - the CONTROL plane (create / delete / suspend tenant, reconcile, seen-users)
      is gated by ``RequirePlatformAdmin`` (bypass OR platform ``superadmin`` —
      bh-main keeps a data-unrestricted superadmin);
    - per-tenant operations (view / manage indexes / manage members) are gated by
      the local guard helpers below, which additionally admit a tenant's OWN
      ``state_admin`` (self-service) without granting cross-tenant reach.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from . import db
from .auth.deps import CurrentUser, RequirePlatformAdmin
from .auth.models import AuthUser
from .auth.tenancy import normalize_instance

router = APIRouter(tags=["tenants"])

# Logical index name charset — mirrors ``db._INDEX_NAME_RE`` (kept local so this
# file depends only on public contracts). Deliberately WITHOUT ``-`` so the single
# ``-`` joining instance and name in a physical collection name stays unambiguous.
_INDEX_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")

# Instance id charset (matches ``main``'s POST /tenants validation).
_INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


# =============================================================================
# Request models
# =============================================================================


class TenantCreate(BaseModel):
    # UI sends ``id``; ``instance`` accepted as an alias for symmetry with the
    # path segment. Either resolves the tenant id.
    id: Optional[str] = None
    instance: Optional[str] = None
    display_name: Optional[str] = None


class IndexCreate(BaseModel):
    name: str
    # Optional physical collection name. When omitted the registry computes it
    # via ensure_tenant_default_index (never computed in this file).
    marqo_index: Optional[str] = None
    embedding_model: Optional[str] = None
    settings: Optional[dict] = None


class MemberCreate(BaseModel):
    email_or_user_id: str
    role: str


class MemberDelete(BaseModel):
    email_or_user_id: str
    role: Optional[str] = None


# =============================================================================
# Response shaping helpers
# =============================================================================


def _index_row_response(row: Optional[dict]) -> Optional[dict]:
    """Normalize a ``tenant_indexes`` row for the UI.

    Exposes the physical collection under both ``marqo_index`` (verbatim column,
    parity with ``main``) and ``physical_index`` (bh-main-friendly alias).
    """
    if not row:
        return None
    physical = row.get("marqo_index")
    return {
        "instance": row.get("instance"),
        "name": row.get("name"),
        "marqo_index": physical,
        "physical_index": physical,
        "embedding_model": row.get("embedding_model"),
        "is_default": bool(row.get("is_default")),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
    }


def _tenant_row_response(row: dict, *, registered: bool = True) -> dict:
    """Normalize a ``tenants`` row for the UI.

    The UI keys rows on ``id`` (tolerating ``instance`` as a fallback), so both
    are emitted with the same value.
    """
    tenant_id = row.get("id") or row.get("instance")
    return {
        "id": tenant_id,
        "instance": tenant_id,
        "display_name": row.get("display_name"),
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "registered": registered,
        "unregistered": not registered,
    }


def _seen_user_response(row: dict) -> dict:
    return {
        "user_id": row.get("user_id"),
        "username": row.get("username"),
        "email": row.get("email"),
    }


def _member_response(row: dict, seen_by_id: Optional[dict] = None) -> dict:
    """Enrich a membership row with directory identity (for the UI list)."""
    uid = row.get("user_id")
    seen = (seen_by_id or {}).get(uid) or {}
    return {
        "user_id": uid,
        "instance": row.get("instance"),
        "role": row.get("role"),
        "username": seen.get("username"),
        "email": seen.get("email"),
        "added_by": row.get("added_by"),
        "created_at": row.get("created_at"),
    }


# =============================================================================
# Guard helpers (local — built on the auth contracts)
# =============================================================================


def _is_tenant_admin(user: AuthUser, inst: str) -> bool:
    """True when the caller is the tenant's OWN admin (``state_admin`` in ``inst``).

    ``roles_in`` already unions the JWT ``tenant_roles``/``groups`` claim with the
    app-side ``tenant_members`` store, and returns every role for a
    data-unrestricted caller.
    """
    return "state_admin" in {r.strip().lower() for r in user.roles_in(inst)}


def _is_tenant_member(user: AuthUser, inst: str) -> bool:
    """True when the caller holds ANY role in ``inst`` (admin / curator / viewer)."""
    return bool(user.roles_in(inst))


def _assert_can_view_tenant(user: AuthUser, instance: str | None) -> str:
    """View gate: platform admin OR any member of the tenant.

    A non-member gets 404 (existence is not leaked). Returns the normalized id.
    """
    inst = normalize_instance(instance)
    if user.is_platform_admin:
        return inst
    if _is_tenant_member(user, inst):
        return inst
    raise HTTPException(404, "Tenant not found")


def _assert_can_manage_indexes(user: AuthUser, instance: str | None) -> str:
    """Index create/delete gate: platform admin OR the tenant's own ``state_admin``.

    A reachable-but-insufficient member gets 403; a non-member gets 404 (no leak).
    """
    inst = normalize_instance(instance)
    if user.is_platform_admin:
        return inst
    if _is_tenant_admin(user, inst):
        return inst
    if _is_tenant_member(user, inst):
        raise HTTPException(403, "Managing a tenant's indexes requires state_admin in that tenant")
    raise HTTPException(404, "Tenant not found")


def _assert_can_manage_members(user: AuthUser, instance: str | None) -> str:
    """Membership gate: platform admin OR the tenant's own ``state_admin``.

    (The tenant's own admin manages its member roster; a pure platform superadmin
    can manage any tenant's members.) 403 for a reachable non-admin, 404 for a
    non-member.
    """
    inst = normalize_instance(instance)
    if user.is_platform_admin:
        return inst
    if _is_tenant_admin(user, inst):
        return inst
    if _is_tenant_member(user, inst):
        raise HTTPException(403, "Managing a tenant's members requires state_admin in that tenant")
    raise HTTPException(404, "Tenant not found")


def _assert_can_view_seen_users(user: AuthUser) -> None:
    """Seen-users picker gate: platform admin OR a tenant admin of ANY tenant."""
    if user.is_platform_admin:
        return
    # Any app-side / claim-derived tenant-admin membership qualifies.
    for roles in (user.tenant_roles or {}).values():
        if "state_admin" in {r.strip().lower() for r in roles}:
            return
    # Legacy flat-claim mode: a state_admin role scoped to some instance.
    flat = {r.strip().lower() for r in user.roles}
    if "state_admin" in flat and user.instances:
        return
    raise HTTPException(403, "Platform admin or tenant admin required")


def _resolve_member_identifier(identifier: str) -> tuple[str, Optional[str], Optional[str]]:
    """Resolve a member identifier to ``(user_id, username, email)``.

    Accepts a user_id, an email, or a username. An email/username is resolved
    against the local ``seen_users`` directory; when unknown, the raw string is
    used verbatim as the user_id (so a pending grant keyed by email/username can
    later be revoked with the same identifier).
    """
    raw = (identifier or "").strip()
    if not raw:
        return "", None, None
    if "@" in raw:
        found = db.find_user_by_email(raw)
        if found:
            return found.get("user_id"), found.get("username"), found.get("email")
        return raw, None, raw
    # Try username match against the directory before falling back to raw id.
    for seen in db.list_seen_users():
        if (seen.get("username") or "").strip().lower() == raw.lower():
            return seen.get("user_id"), seen.get("username"), seen.get("email")
        if (seen.get("user_id") or "") == raw:
            return seen.get("user_id"), seen.get("username"), seen.get("email")
    return raw, None, None


# =============================================================================
# Tenant registry (control plane)
# =============================================================================


@router.get("/tenants")
async def list_tenants_route(user: RequirePlatformAdmin):
    """List the tenant registry, merged with instances seen only in documents.

    Registered tenants (a ``tenants`` row) are flagged ``registered: true``.
    Instances that exist de-facto (own documents / index rows) but were never
    registered still appear, flagged ``registered: false`` / ``unregistered:
    true`` so the superadmin console surfaces them for reconcile.
    """
    rows = db.list_tenants()
    registered_ids = {r.get("id") for r in rows}
    result = [_tenant_row_response(r, registered=True) for r in rows]
    for inst in db.list_known_instances():
        if inst not in registered_ids:
            result.append(
                _tenant_row_response(
                    {"id": inst, "display_name": None, "status": None, "created_at": None},
                    registered=False,
                )
            )
    return result


@router.post("/tenants")
async def create_tenant_route(payload: TenantCreate, user: RequirePlatformAdmin):
    """Create (idempotently) a tenant + its own default index/collection.

    Body: ``{id, display_name?}`` (``instance`` accepted as an alias for ``id``).
    Writes the ``tenants`` registry row (upsert — a re-create refreshes the
    display name without clobbering existing data) and provisions the tenant's
    OWN default index via ``ensure_tenant_default_index`` so a brand-new tenant
    immediately has an isolated collection. Returns the resulting tenant + its
    default index.
    """
    instance = (payload.id or payload.instance or "").strip().lower()
    if not instance:
        raise HTTPException(400, "id (tenant instance) is required")
    if not _INSTANCE_RE.fullmatch(instance):
        raise HTTPException(400, "id must be lowercase alphanumeric with - or _ (leading alnum)")

    already = bool(db.get_tenant(instance))
    tenant = db.create_tenant(instance, display_name=payload.display_name)

    # Provision the tenant's OWN default collection (idempotent; never returns
    # another tenant's collection). Registers the default index row if missing.
    db.ensure_tenant_default_index(instance)
    default_row = db.get_tenant_index(instance, "default")

    return {
        **_tenant_row_response(tenant, registered=True),
        "default_index": _index_row_response(default_row),
        "created": not already,
        "adopted": already,
    }


@router.post("/tenants/reconcile")
async def reconcile_tenants_route(user: RequirePlatformAdmin):
    """Backfill the registry from instances seen in documents / index rows.

    For every instance in ``list_known_instances()`` that lacks a ``tenants`` row,
    insert one (idempotent — never clobbers a curated row); and for any such
    tenant with no default index, provision one via ``ensure_tenant_default_index``.
    Registry-only + non-destructive. Takes NO body. Returns a summary of what was
    created plus the resulting tenant list.
    """
    created_tenants: list[str] = []
    created_indexes: list[str] = []
    for inst in db.list_known_instances():
        if not db.get_tenant(inst):
            db.create_tenant_row(inst, display_name=inst)
            created_tenants.append(inst)
        if not db.get_tenant_index(inst, "default"):
            db.ensure_tenant_default_index(inst)
            created_indexes.append(inst)
    tenants = db.list_tenants()
    return {
        "reconciled": tenants,
        "created_tenants": created_tenants,
        "created_indexes": created_indexes,
        "count": len(created_tenants),
    }


@router.get("/tenants/_seen_users")
async def list_seen_users_route(user: CurrentUser):
    """Directory of users the app has observed — powers the add-member picker.

    Gated: platform admin OR a tenant admin of any tenant. Returns
    ``[{user_id, username, email}]``.
    """
    _assert_can_view_seen_users(user)
    return [_seen_user_response(u) for u in db.list_seen_users()]


@router.get("/tenants/{instance}")
async def get_tenant_route(instance: str, user: CurrentUser):
    """Fetch a single tenant (registry row + its indexes).

    Gated by :func:`_assert_can_view_tenant`. A known-but-unregistered instance
    (docs only, no ``tenants`` row) is returned with ``registered: false`` rather
    than 404 so a tenant admin/member can still see it; a truly unknown instance
    is 404.
    """
    inst = _assert_can_view_tenant(user, instance)
    row = db.get_tenant(inst)
    if row:
        tenant = _tenant_row_response(row, registered=True)
    elif inst in set(db.list_known_instances()):
        tenant = _tenant_row_response(
            {"id": inst, "display_name": None, "status": None, "created_at": None},
            registered=False,
        )
    else:
        raise HTTPException(404, "Tenant not found")
    tenant["indexes"] = [_index_row_response(r) for r in db.list_tenant_indexes(inst)]
    return tenant


# =============================================================================
# Tenant indexes (per-tenant, self-service)
# =============================================================================


@router.get("/tenants/{instance}/indexes")
async def list_tenant_indexes_route(instance: str, user: CurrentUser):
    """List a tenant's logical indexes. Gated: any member of the tenant."""
    inst = _assert_can_view_tenant(user, instance)
    return [_index_row_response(r) for r in db.list_tenant_indexes(inst)]


@router.post("/tenants/{instance}/indexes")
async def create_tenant_index_route(instance: str, payload: IndexCreate, user: CurrentUser):
    """Provision an additional logical index within a tenant.

    Body: ``{name, marqo_index?, embedding_model?, settings?}``. When
    ``marqo_index`` is omitted the physical collection name is computed by the
    registry (``ensure_tenant_default_index``). A caller-supplied ``marqo_index``
    is rejected (409) if it already maps to a DIFFERENT ``(instance, name)``.
    Gated: platform admin OR the tenant's own ``state_admin``.
    """
    inst = _assert_can_manage_indexes(user, instance)
    name = (payload.name or "").strip().lower()
    if not name:
        raise HTTPException(400, "name is required")
    if not _INDEX_NAME_RE.fullmatch(name):
        raise HTTPException(400, "name must match ^[a-z0-9_]{1,40}$ (letters, digits, _ only)")
    if db.get_tenant_index(inst, name):
        raise HTTPException(409, f"Index '{name}' already exists for tenant '{inst}'")

    physical = (payload.marqo_index or "").strip()
    if physical:
        # Explicit physical name: cross-tenant collision guard.
        collision = db.get_index_by_physical_name(physical)
        if collision and (collision.get("instance") != inst or collision.get("name") != name):
            raise HTTPException(
                409,
                f"Physical collection '{physical}' is already registered to "
                f"{collision.get('instance')}/{collision.get('name')}",
            )
        is_first = not db.list_tenant_indexes(inst)
        row = db.create_index_row(
            instance=inst,
            name=name,
            marqo_index=physical,
            embedding_model=payload.embedding_model or None,
            settings_json=None,
            is_default=is_first,
        )
    else:
        # Let the registry compute + register the physical collection.
        try:
            db.ensure_tenant_default_index(inst, name)
        except sqlite3.IntegrityError as exc:
            # A UNIQUE(marqo_index) violation ⇒ the computed physical name already
            # belongs to another registry row.
            raise HTTPException(409, f"Physical collection already registered: {exc}") from exc
        row = db.get_tenant_index(inst, name)

    return _index_row_response(row)


@router.delete("/tenants/{instance}/indexes/{name}")
async def delete_tenant_index_route(
    instance: str,
    name: str,
    user: CurrentUser,
    force: bool = Query(False, description="Delete even when the index still has documents"),
):
    """Delete a tenant's logical index (registry row), keyed by logical ``name``.

    Refuses (409) to drop a non-empty index unless ``?force=true``. The physical
    Qdrant collection is intentionally left intact here (collection lifecycle is
    owned by the vector-store layer / ingest path, not this control-plane route).
    Gated: platform admin OR the tenant's own ``state_admin``.
    """
    inst = _assert_can_manage_indexes(user, instance)
    logical = (name or "").strip().lower()
    row = db.get_tenant_index(inst, logical)
    if not row:
        raise HTTPException(404, "Index not found")

    doc_count = db.count_documents_for_index(
        inst, logical, include_default_null=bool(row.get("is_default"))
    )
    if doc_count > 0 and not force:
        raise HTTPException(
            409,
            f"Index '{logical}' still has {doc_count} document(s). Pass ?force=true to delete anyway.",
        )

    deleted = db.delete_index_row(inst, logical)
    return {
        "instance": inst,
        "name": logical,
        "marqo_index": row.get("marqo_index"),
        "deleted": deleted,
        "documents": doc_count,
        "forced": bool(force),
    }


# =============================================================================
# Tenant members (per-tenant roster)
# =============================================================================


@router.get("/tenants/{instance}/members")
async def list_tenant_members_route(instance: str, user: CurrentUser):
    """List a tenant's members, enriched with directory identity.

    Gated: platform admin OR the tenant's own ``state_admin``.
    """
    inst = _assert_can_manage_members(user, instance)
    seen_by_id = {u.get("user_id"): u for u in db.list_seen_users()}
    return [_member_response(m, seen_by_id) for m in db.list_tenant_members(inst)]


def _member_state(inst: str) -> list[dict]:
    seen_by_id = {u.get("user_id"): u for u in db.list_seen_users()}
    return [_member_response(m, seen_by_id) for m in db.list_tenant_members(inst)]


@router.post("/tenants/{instance}/members")
async def add_tenant_member_route(instance: str, payload: MemberCreate, user: CurrentUser):
    """Grant a tenant-scoped role to a user (by email or user_id).

    Body: ``{email_or_user_id, role}`` where ``role`` ∈
    ``{state_admin, content_curator, viewer}``. An email is resolved via the local
    ``seen_users`` directory; when unknown the raw string is stored as a PENDING
    grant keyed by the email/user_id (revocable with the same identifier). Gated:
    platform admin OR the tenant's own ``state_admin``. Idempotent — returns the
    resulting member roster.
    """
    inst = _assert_can_manage_members(user, instance)
    role = (payload.role or "").strip().lower()
    if role not in db.VALID_TENANT_ROLES:
        raise HTTPException(
            400, f"role must be one of {', '.join(sorted(db.VALID_TENANT_ROLES))}"
        )
    raw = (payload.email_or_user_id or "").strip()
    if not raw:
        raise HTTPException(400, "email_or_user_id is required")

    user_id, _username, _email = _resolve_member_identifier(raw)
    if not user_id:
        raise HTTPException(400, "could not resolve a user id from the identifier")

    member = db.add_tenant_member(user_id, inst, role, added_by=user.user_id)
    if member is None:
        # add_tenant_member returns None on an invalid role / empty id — role was
        # already validated, so this is a bad identifier.
        raise HTTPException(400, "invalid member (unresolvable user or role)")

    seen_by_id = {u.get("user_id"): u for u in db.list_seen_users()}
    return {
        "added": _member_response(member, seen_by_id),
        "members": _member_state(inst),
    }


@router.delete("/tenants/{instance}/members/{identifier}")
async def remove_tenant_member_route(
    instance: str,
    identifier: str,
    user: CurrentUser,
    role: Optional[str] = Query(None, description="Revoke only this role; omit to revoke all"),
):
    """Revoke a member (path form). ``{identifier}`` = user_id, email, or username.

    Omitting ``?role=`` removes every role the user holds on the tenant. Gated:
    platform admin OR the tenant's own ``state_admin``. Returns the resulting roster.
    """
    inst = _assert_can_manage_members(user, instance)
    user_id, _username, _email = _resolve_member_identifier(identifier)
    if not user_id:
        raise HTTPException(400, "could not resolve a user id from the identifier")
    role_norm = (role or "").strip().lower() or None
    removed = db.remove_tenant_member(user_id, inst, role_norm)
    return {
        "instance": inst,
        "user_id": user_id,
        "role": role_norm,
        "removed": removed,
        "members": _member_state(inst),
    }


@router.delete("/tenants/{instance}/members")
async def remove_tenant_member_body_route(instance: str, payload: MemberDelete, user: CurrentUser):
    """Revoke a member (body form): ``{email_or_user_id, role?}``.

    Convenience alternative to the path form; same gate + semantics.
    """
    inst = _assert_can_manage_members(user, instance)
    user_id, _username, _email = _resolve_member_identifier(payload.email_or_user_id)
    if not user_id:
        raise HTTPException(400, "could not resolve a user id from the identifier")
    role_norm = (payload.role or "").strip().lower() or None
    removed = db.remove_tenant_member(user_id, inst, role_norm)
    return {
        "instance": inst,
        "user_id": user_id,
        "role": role_norm,
        "removed": removed,
        "members": _member_state(inst),
    }
