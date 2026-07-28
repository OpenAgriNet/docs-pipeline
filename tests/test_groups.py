"""Unit tests for Keycloak group path → tenant / role parsing."""

from __future__ import annotations

from pipeline.auth.groups import (
    ROLE_STATE_ADMIN,
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


def test_legacy_contributor_reviewer_paths():
    access = parse_group_paths(
        [
            "/states/MH/contributor",
            "/states/UP/reviewer",
        ]
    )
    assert access.state_roles == {"mh": ROLE_STATE_ADMIN, "up": ROLE_STATE_VIEW}


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
            "realm_access": {"roles": ["content_curator"]},
            "instances": ["tenant-a"],
        }
    )
    assert user.instances == ["tenant-a"]
    assert Permission.UPLOAD in user.permissions
    assert user_can_access_instance(user, "tenant-a")
    assert not user_can_access_instance(user, "mh")
