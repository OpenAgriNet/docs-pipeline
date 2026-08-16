"""Unit tests for Keycloak group path → tenant / role parsing."""

from __future__ import annotations

from pipeline.auth.groups import (
    ROLE_BH_VIEWER,
    ROLE_STATE_ADMIN,
    ROLE_STATE_APPROVER,
    ROLE_STATE_CONTRIBUTOR,
    ROLE_STATE_VIEW,
    ROLE_SUPER_ADMIN,
    parse_group_paths,
    role_for_instance,
)
from pipeline.auth.jwt import claims_to_user
from pipeline.auth.permissions import Permission, UserRole, permissions_for_roles
from pipeline.auth.tenancy import allowed_instances, user_can_access_instance


def test_parse_global_super_admin():
    access = parse_group_paths(["/global/super-admin"])
    assert access.is_super_admin is True
    assert ROLE_SUPER_ADMIN in access.roles
    assert access.instances == []


def test_parse_multi_state_roles():
    access = parse_group_paths(
        [
            "/states/MH/admin",
            "/states/UP/view",
        ]
    )
    assert access.is_super_admin is False
    assert access.state_roles == {"mh": ROLE_STATE_ADMIN, "up": ROLE_STATE_VIEW}
    assert set(access.instances) == {"mh", "up"}
    assert role_for_instance(access, "MH") == ROLE_STATE_ADMIN
    assert role_for_instance(access, "up") == ROLE_STATE_VIEW
    assert role_for_instance(access, "bh") is None


def test_approver_and_contributor_paths():
    access = parse_group_paths(
        [
            "/states/MH/approver",
            "/states/UP/contributor",
            "/states/BH/view",
        ]
    )
    assert access.state_roles == {
        "mh": ROLE_STATE_APPROVER,
        "up": ROLE_STATE_CONTRIBUTOR,
        "bh": ROLE_STATE_VIEW,
    }
    # ``contributor`` is the weakest upload role, never state_admin.
    assert access.state_roles["up"] != ROLE_STATE_ADMIN


def test_contributor_approves_stages_but_cannot_publish_to_dev():
    """The one gate a contributor must not pass: approve-ingestion (DEV publish)."""
    user = claims_to_user({"sub": "c", "groups": ["/states/UP/contributor"]})

    # OCR / translation / chunk approvals all ride on REVIEW.
    assert user.has_permission(Permission.REVIEW)
    assert user.has_permission_for_instance(Permission.REVIEW, "up")
    # …but publishing to DEV is its own permission.
    assert not user.has_permission(Permission.APPROVE_INGESTION)
    assert not user.has_permission_for_instance(Permission.APPROVE_INGESTION, "up")
    assert not user.has_permission(Permission.DELETE_OWN)

    # Approver and admin both clear the DEV publish gate.
    for group, role in (("approver", "state_approver"), ("admin", "state_admin")):
        stronger = claims_to_user({"sub": role, "groups": [f"/states/UP/{group}"]})
        assert stronger.has_permission(Permission.APPROVE_INGESTION), role
        assert stronger.has_permission_for_instance(Permission.APPROVE_INGESTION, "up"), role

    # Only the admin may delete.
    approver = claims_to_user({"sub": "a", "groups": ["/states/UP/approver"]})
    admin = claims_to_user({"sub": "d", "groups": ["/states/UP/admin"]})
    assert not approver.has_permission(Permission.DELETE_OWN)
    assert admin.has_permission(Permission.DELETE_OWN)


def test_only_state_admin_and_super_admin_hold_delete_own():
    """delete_own gates the delete route; approver/contributor must not have it."""
    holds = {
        "/global/super-admin": True,
        "/states/UP/admin": True,
        "/states/UP/approver": False,
        "/states/UP/contributor": False,
        "/states/UP/view": False,
    }
    for group, expected in holds.items():
        user = claims_to_user({"sub": group, "groups": [group]})
        assert user.has_permission(Permission.DELETE_OWN) is expected, group

    # Only the super admin carries ADMIN, which is what allows purge and
    # deleting documents someone else uploaded.
    assert claims_to_user({"sub": "s", "groups": ["/global/super-admin"]}).has_permission(
        Permission.ADMIN
    )
    assert not claims_to_user({"sub": "a", "groups": ["/states/UP/admin"]}).has_permission(
        Permission.ADMIN
    )


def test_bh_viewer_reads_every_state_without_write():
    access = parse_group_paths(["/global/bh-viewer"])
    assert access.is_bh_viewer is True
    assert access.is_super_admin is False
    assert access.state_roles == {}
    # No per-state group, but any state resolves to view.
    assert role_for_instance(access, "mh") == ROLE_STATE_VIEW
    assert role_for_instance(access, "wb") == ROLE_STATE_VIEW


def test_bh_viewer_keeps_stronger_state_role():
    access = parse_group_paths(["/global/bh-viewer", "/states/MH/admin"])
    assert role_for_instance(access, "mh") == ROLE_STATE_ADMIN
    assert role_for_instance(access, "up") == ROLE_STATE_VIEW


def test_higher_role_wins_same_state():
    access = parse_group_paths(
        [
            "/states/MH/view",
            "/states/MH/admin",
        ]
    )
    assert access.state_roles["mh"] == ROLE_STATE_ADMIN


def test_hyphen_and_case_aliases():
    access = parse_group_paths(["/Global/Super-Admin", "/states/mh/Admin"])
    assert access.is_super_admin is True
    # Super-admin short-circuits state map for product roles list
    assert ROLE_SUPER_ADMIN in access.roles


def test_product_role_permissions():
    assert Permission.MANAGE_USERS in permissions_for_roles([UserRole.SUPER_ADMIN.value])
    admin = permissions_for_roles([UserRole.STATE_ADMIN.value])
    assert Permission.UPLOAD in admin
    assert Permission.DELETE_OWN in admin
    assert Permission.REVIEW in admin
    assert Permission.MANAGE_USERS not in admin
    view = permissions_for_roles([UserRole.STATE_VIEW.value])
    assert Permission.SEARCH in view
    assert Permission.REVIEW not in view
    assert Permission.UPLOAD not in view
    assert Permission.DELETE_OWN not in view
    # legacy names still map
    assert Permission.UPLOAD in permissions_for_roles(["contributor"])
    assert Permission.SEARCH in permissions_for_roles(["reviewer"])
    assert Permission.UPLOAD not in permissions_for_roles(["reviewer"])


def test_claims_to_user_from_groups_only():
    user = claims_to_user(
        {
            "sub": "u-multi",
            "preferred_username": "multi",
            "email": "multi@example.com",
            "groups": [
                "/states/MH/admin",
                "/states/UP/view",
            ],
            # no realm_access roles required when groups present
        }
    )
    assert user.user_id == "u-multi"
    assert set(user.instances) == {"mh", "up"}
    assert user.state_roles["mh"] == "state_admin"
    assert user.state_roles["up"] == "state_view"
    assert user.is_superadmin is False
    assert allowed_instances(user) == {"mh", "up"}
    assert user_can_access_instance(user, "mh")
    assert not user_can_access_instance(user, "bh")
    # Union of state_admin + state_view permissions
    assert Permission.UPLOAD in user.permissions
    assert Permission.SEARCH in user.permissions
    assert Permission.MANAGE_USERS not in user.permissions
    assert user.has_permission_for_instance(Permission.UPLOAD, "mh") is True
    assert user.has_permission_for_instance(Permission.UPLOAD, "up") is False
    assert user.has_permission_for_instance(Permission.SEARCH, "up") is True
    assert user.has_permission_for_instance(Permission.REVIEW, "up") is False


def test_claims_super_admin_unrestricted():
    user = claims_to_user(
        {
            "sub": "sa",
            "groups": ["/global/super-admin"],
            "realm_access": {"roles": ["super_admin"]},
        }
    )
    assert user.is_superadmin is True
    assert allowed_instances(user) is None
    assert user_can_access_instance(user, "mh")
    assert Permission.MANAGE_USERS in user.permissions


def test_legacy_instances_claim_still_works():
    """Tokens without groups still scope via instances attribute claim."""
    user = claims_to_user(
        {
            "sub": "legacy",
            "realm_access": {"roles": ["state_admin"]},
            "instances": ["tenant-a"],
        }
    )
    assert user.instances == ["tenant-a"]
    assert Permission.UPLOAD in user.permissions
    assert user_can_access_instance(user, "tenant-a")
    assert not user_can_access_instance(user, "mh")
