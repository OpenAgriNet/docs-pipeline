"""Auth plumbing: claim parsing, permissions, bypass mode, guards, JWT validation."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.requests import Request

from pipeline.app import app
from pipeline.auth.config import AuthConfig, validate_auth_config
from pipeline.auth.deps import get_current_user, require_permission
from pipeline.auth.jwt import claims_to_user, clear_jwks_cache, decode_and_validate_token
from pipeline.auth.models import local_bypass_user
from pipeline.auth.permissions import Permission, permissions_for_roles
from tests.helpers import dependency_calls, master_admin, member


def _user(**claims):
    return claims_to_user({"sub": "u1", **claims})


def test_permissions_for_roles():
    assert Permission.UPLOAD in permissions_for_roles(["content_curator"])
    assert Permission.MANAGE_USERS not in permissions_for_roles(["content_curator"])
    # master_admin is a CONTROL-PLANE role: its authority is the realm-role gate, not a data permission.
    assert permissions_for_roles(["master_admin"]) == set()
    assert permissions_for_roles(["viewer"]) == {Permission.SEARCH}
    assert permissions_for_roles(["unknown-role"]) == set()


def test_claims_to_user_maps_keycloak_shape():
    user = _user(
        preferred_username="aayush", email="aayush@example.com", realm_access={"roles": ["content_curator"]},
        instances=["tenant-a", "tenant-b"], envs=["dev"],
    )
    assert user.user_id == "u1"
    assert user.username == "aayush"
    assert Permission.UPLOAD in user.permissions
    assert user.instances == ["tenant-a", "tenant-b"]
    assert user.envs == ["dev"]
    assert user.has_instance("Tenant-A")
    assert not user.has_env("prod")


def test_tenant_roles_claim_parses_into_per_instance_map():
    user = _user(tenant_roles={"tenant-a": ["content_curator"], "Tenant-B": ["viewer"]})
    assert user.tenant_roles == {"tenant-a": {"content_curator"}, "tenant-b": {"viewer"}}, "lowercased"
    assert set(user.instances) == {"tenant-a", "tenant-b"}
    # The any-instance flat view unions every tenant's roles.
    assert Permission.UPLOAD in user.permissions
    assert Permission.SEARCH in user.permissions


def test_groups_claim_parses_into_same_map():
    user = _user(groups=["/tenant-a/content_curator", "/tenant-b/viewer"])
    assert user.tenant_roles == {"tenant-a": {"content_curator"}, "tenant-b": {"viewer"}}
    assert set(user.instances) == {"tenant-a", "tenant-b"}


def test_tenant_roles_and_groups_merge():
    user = _user(tenant_roles={"tenant-a": ["viewer"]}, groups=["/tenant-a/content_curator", "/tenant-b/viewer"])
    assert user.tenant_roles["tenant-a"] == {"viewer", "content_curator"}
    assert user.tenant_roles["tenant-b"] == {"viewer"}


def test_permissions_in_is_per_instance():
    """Curator-in-A / viewer-in-B: may curate A but only search B."""
    user = member({"tenant-a": ["content_curator"], "tenant-b": ["viewer"]})
    assert {Permission.UPLOAD, Permission.REVIEW, Permission.SEARCH} <= user.permissions_in("tenant-a")
    assert user.permissions_in("tenant-b") == {Permission.SEARCH}
    assert Permission.REVIEW not in user.permissions_in("tenant-b")
    assert Permission.UPLOAD not in user.permissions_in("tenant-b")
    assert user.permissions_in("tenant-c") == set()


def test_resource_access_master_admin_does_not_elevate():
    """L1: a CLIENT role named master_admin, or a flat ``roles`` entry, never grants
    platform admin; only ``realm_access.roles`` does."""
    spoof_client = _user(
        resource_access={"some-client": {"roles": ["master_admin"]}}, tenant_roles={"tenant-a": ["viewer"]}
    )
    assert spoof_client.realm_roles == []
    assert spoof_client.is_admin is False
    assert spoof_client.is_platform_admin is False
    assert spoof_client.is_instance_unrestricted() is False
    assert spoof_client.permissions_in("tenant-b") == set(), "still scoped to its own tenant"

    spoof_flat = _user(roles=["master_admin"])
    assert spoof_flat.is_admin is False
    assert spoof_flat.is_platform_admin is False

    # The genuine realm master_admin manages tenants but never reads tenant data.
    real = master_admin()
    assert real.realm_roles == ["master_admin"]
    assert real.is_admin is True
    assert real.is_platform_admin is True
    assert real.is_instance_unrestricted() is False


def test_master_admin_is_control_plane_only_no_data():
    """Its data scope is exactly its tenant membership."""
    pure = master_admin()
    assert pure.is_platform_admin is True
    assert pure.is_instance_unrestricted() is False
    assert pure.permissions == set()
    assert pure.permissions_in("tenant-a") == set()
    assert pure.permissions_in("tenant-z") == set()

    # Also a viewer in tenant-a: the control plane plus exactly that viewer scope.
    both = _user(realm_access={"roles": ["master_admin"]}, tenant_roles={"tenant-a": ["viewer"]})
    assert both.is_platform_admin is True
    assert both.is_instance_unrestricted() is False
    assert both.permissions_in("tenant-a") == {Permission.SEARCH}
    assert both.permissions_in("tenant-z") == set()


def test_flat_claim_back_compat_roles_apply_across_instances():
    """Legacy flat claims (no tenant_roles/groups) apply their roles to every claimed instance."""
    user = _user(realm_access={"roles": ["content_curator"]}, instances=["tenant-a", "tenant-b"])
    assert user.tenant_roles == {}
    assert Permission.REVIEW in user.permissions_in("tenant-a")
    assert Permission.REVIEW in user.permissions_in("tenant-b")
    assert user.permissions_in("tenant-c") == set()


def test_unknown_group_role_mints_no_membership():
    """Fix 7: a spoofed ``/x/superuser`` group mints neither membership in ``x`` nor a role
    (the old parser took any ``/a/b`` as ``{a: {b}}``); a well-formed group still works."""
    user = _user(groups=["/x/superuser", "/tenant-a/admin"])
    assert user.tenant_roles == {"tenant-a": {"admin"}}
    assert "x" not in {i.lower() for i in user.instances}
    assert user.permissions_in("x") == set()
    assert user.permissions_in("tenant-a") == set(Permission)


def test_unknown_role_in_tenant_roles_object_is_dropped():
    user = _user(tenant_roles={"tenant-a": ["superuser", "viewer"]})
    assert user.tenant_roles == {"tenant-a": {"viewer"}}
    assert user.permissions_in("tenant-a") == {Permission.SEARCH}


def test_tenant_roles_object_with_only_unknown_role_mints_no_membership():
    user = _user(tenant_roles={"tenant-a": ["superuser"]})
    assert user.tenant_roles == {}
    assert "tenant-a" not in {i.lower() for i in user.instances}
    assert user.permissions_in("tenant-a") == set()


def test_group_prefix_constrains_accepted_paths(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_TENANT_GROUP_PREFIX", "/tenants")
    user = _user(groups=["/tenants/tenant-a/admin", "/other/tenant-b/admin", "/tenant-c/viewer"])
    assert user.tenant_roles == {"tenant-a": {"admin"}}, "only paths under the prefix count"


def test_group_prefix_unset_is_backcompat():
    user = _user(groups=["/tenant-a/admin"])
    assert user.tenant_roles == {"tenant-a": {"admin"}}
    assert user.permissions_in("tenant-a") == set(Permission)


def test_local_bypass_has_all_permissions():
    user = local_bypass_user()
    assert user.token_disabled_mode is True
    assert set(Permission).issubset(user.permissions)


@pytest.mark.parametrize("disabled", [False, True], ids=["enabled-requires-keycloak", "disabled-does-not"])
def test_startup_keycloak_config_is_required_only_when_auth_is_enabled(disabled):
    config = AuthConfig(
        disabled=disabled, keycloak_issuer="", keycloak_audience="docs-pipeline-api", keycloak_jwks_url=""
    )
    if disabled:
        validate_auth_config(config)
    else:
        with pytest.raises(RuntimeError, match="KEYCLOAK_ISSUER.*KEYCLOAK_JWKS_URL"):
            validate_auth_config(config)


@pytest.mark.asyncio
async def test_get_current_user_bypass_mode():
    request = Request({"type": "http", "method": "GET", "path": "/auth/me", "headers": [], "query_string": b""})
    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        user = await get_current_user(request)
    assert user.user_id == "local-dev"
    assert user.has_permission(Permission.UPLOAD)


@pytest.mark.asyncio
async def test_require_permission_forbidden():
    with pytest.raises(HTTPException) as exc:
        await require_permission(Permission.ADMIN)(user=_user(realm_access={"roles": ["viewer"]}))
    assert exc.value.status_code == 403


def test_every_route_is_gated_or_explicitly_classified():
    """Only ``/health`` (plus framework docs) may be public: gate a new route rather than widen the allowlist."""
    public = {("GET", "/health")}
    framework_paths = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    ungated = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or getattr(route, "dependant", None) is None or path in framework_paths:
            continue
        if get_current_user not in dependency_calls(route):
            methods = (route.methods or set()) - {"HEAD", "OPTIONS"}
            ungated += [f"{method} {path}" for method in methods if (method, path) not in public]
    assert not ungated, f"neither gated nor on the public allowlist (only /health may be public): {sorted(ungated)}"


def test_permission_values_and_str_enum_compat():
    """Permission works as a plain ``str`` on Python 3.10 (no StrEnum)."""
    assert [p.value for p in (Permission.UPLOAD, Permission.REVIEW, Permission.PIPELINE, Permission.ADMIN)] == [
        "upload", "review", "pipeline", "admin",
    ]
    assert isinstance(Permission.UPLOAD, str)
    assert Permission.UPLOAD == "upload"
    assert Permission("upload") is Permission.UPLOAD


ISSUER, AUDIENCE = "https://example.com/realms/test", "docs-pipeline-api"


def _rsa_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


class _Jwks:
    """Stands in for ``PyJWKClient``: every token's signing key is ``key``."""

    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, _token):
        return self  # exposes ``.key``, like a ``PyJWK``


def test_decode_and_validate_token_requires_exp_and_rejects_bad_sig():
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    config = AuthConfig(
        disabled=False, keycloak_issuer=ISSUER, keycloak_audience=AUDIENCE,
        keycloak_jwks_url=f"{ISSUER}/protocol/openid-connect/certs", jwt_leeway_seconds=0,
    )
    claims = {"sub": "u1", "iss": ISSUER, "aud": AUDIENCE, "realm_access": {"roles": ["viewer"]}}
    exp = datetime.now(timezone.utc) + timedelta(hours=1)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    clear_jwks_cache()
    with patch("pipeline.auth.jwt._get_jwks_client", return_value=_Jwks(signing.public_key())):
        no_exp = pyjwt.encode(claims, _rsa_pem(signing), algorithm="RS256")
        with pytest.raises(HTTPException) as missing_exp:
            decode_and_validate_token(no_exp, config)
        assert missing_exp.value.status_code == 401

        bad_sig = pyjwt.encode({**claims, "exp": exp}, _rsa_pem(other), algorithm="RS256")
        with pytest.raises(HTTPException) as bad:
            decode_and_validate_token(bad_sig, config)
        assert bad.value.status_code == 401

        good = {**claims, "exp": exp, "preferred_username": "alice", "instances": ["tenant-a"]}
        user = decode_and_validate_token(pyjwt.encode(good, _rsa_pem(signing), algorithm="RS256"), config)
        assert user.user_id == "u1"
        assert user.instances == ["tenant-a"]
        assert Permission.SEARCH in user.permissions
