"""Unit tests for auth plumbing (permissions, bypass mode, guards)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from pipeline.auth.config import AuthConfig, load_auth_config, validate_auth_config
from pipeline.auth.deps import get_current_user, require_permission
from pipeline.auth.jwt import claims_to_user
from pipeline.auth.models import local_bypass_user
from pipeline.auth.permissions import Permission, permissions_for_roles


def test_permissions_for_roles():
    assert Permission.UPLOAD in permissions_for_roles(["content_curator"])
    assert Permission.MANAGE_USERS not in permissions_for_roles(["content_curator"])
    # master_admin is a CONTROL-PLANE role: it carries NO data permissions.
    # Its authority is the realm-role platform-admin gate, not a data permission.
    assert permissions_for_roles(["master_admin"]) == set()
    assert permissions_for_roles(["viewer"]) == {Permission.SEARCH}
    assert permissions_for_roles(["unknown-role"]) == set()


def test_claims_to_user_maps_keycloak_shape():
    user = claims_to_user(
        {
            "sub": "user-1",
            "preferred_username": "aayush",
            "email": "aayush@example.com",
            "realm_access": {"roles": ["content_curator"]},
            "instances": ["tenant-a", "tenant-b"],
            "envs": ["dev"],
        }
    )
    assert user.user_id == "user-1"
    assert user.username == "aayush"
    assert Permission.UPLOAD in user.permissions
    assert user.instances == ["tenant-a", "tenant-b"]
    assert user.envs == ["dev"]
    assert user.has_instance("Tenant-A")
    assert not user.has_env("prod")


def test_tenant_roles_claim_parses_into_per_instance_map():
    user = claims_to_user(
        {
            "sub": "u-mt",
            "tenant_roles": {
                "tenant-a": ["content_curator"],
                "Tenant-B": ["viewer"],
            },
        }
    )
    # Instance ids and roles are normalized to lowercase.
    assert user.tenant_roles == {
        "tenant-a": {"content_curator"},
        "tenant-b": {"viewer"},
    }
    # instances == keys(tenant_roles)
    assert set(user.instances) == {"tenant-a", "tenant-b"}
    # Any-instance flat view unions all tenant roles.
    assert Permission.UPLOAD in user.permissions  # from curator in tenant-a
    assert Permission.SEARCH in user.permissions


def test_groups_claim_parses_into_same_map():
    user = claims_to_user(
        {
            "sub": "u-groups",
            "groups": ["/tenant-a/content_curator", "/tenant-b/viewer"],
        }
    )
    assert user.tenant_roles == {
        "tenant-a": {"content_curator"},
        "tenant-b": {"viewer"},
    }
    assert set(user.instances) == {"tenant-a", "tenant-b"}


def test_tenant_roles_and_groups_merge():
    user = claims_to_user(
        {
            "sub": "u-merge",
            "tenant_roles": {"tenant-a": ["viewer"]},
            "groups": ["/tenant-a/content_curator", "/tenant-b/viewer"],
        }
    )
    assert user.tenant_roles["tenant-a"] == {"viewer", "content_curator"}
    assert user.tenant_roles["tenant-b"] == {"viewer"}


def test_permissions_in_is_per_instance():
    """admin-in-A / viewer-in-B: may curate A but only search B."""
    user = claims_to_user(
        {
            "sub": "u-split",
            "tenant_roles": {
                "tenant-a": ["content_curator"],
                "tenant-b": ["viewer"],
            },
        }
    )
    # Tenant A: full curator permissions.
    assert Permission.UPLOAD in user.permissions_in("tenant-a")
    assert Permission.REVIEW in user.permissions_in("tenant-a")
    assert Permission.SEARCH in user.permissions_in("tenant-a")
    # Tenant B: search only — no mutation.
    assert user.permissions_in("tenant-b") == {Permission.SEARCH}
    assert Permission.REVIEW not in user.permissions_in("tenant-b")
    assert Permission.UPLOAD not in user.permissions_in("tenant-b")
    # An unrelated tenant: nothing.
    assert user.permissions_in("tenant-c") == set()


def test_resource_access_master_admin_does_not_elevate():
    """L1: a CLIENT role named master_admin (resource_access) or a flat ``roles``
    entry must NOT grant platform-admin — only realm_access.roles does."""
    spoof_client = claims_to_user(
        {
            "sub": "u-spoof",
            "resource_access": {"some-client": {"roles": ["master_admin"]}},
            "tenant_roles": {"tenant-a": ["viewer"]},
        }
    )
    assert spoof_client.realm_roles == []
    assert spoof_client.is_admin is False
    assert spoof_client.is_platform_admin is False
    assert spoof_client.is_instance_unrestricted() is False
    # It is still scoped to its own tenant only.
    assert spoof_client.permissions_in("tenant-b") == set()

    spoof_flat = claims_to_user({"sub": "u-flat", "roles": ["master_admin"]})
    assert spoof_flat.is_admin is False
    assert spoof_flat.is_platform_admin is False

    # A genuine realm master_admin is a control-plane platform admin, but NOT
    # data-unrestricted: it can manage tenants, never read tenant data.
    real = claims_to_user({"sub": "root", "realm_access": {"roles": ["master_admin"]}})
    assert real.realm_roles == ["master_admin"]
    assert real.is_admin is True
    assert real.is_platform_admin is True
    assert real.is_instance_unrestricted() is False


def test_master_admin_is_control_plane_only_no_data():
    """A real master_admin is the control plane: it holds NO data permissions and
    is NOT data-unrestricted. Its data scope is exactly its tenant membership."""
    # Pure platform admin (no tenant membership): zero data everywhere.
    pure = claims_to_user({"sub": "root", "realm_access": {"roles": ["master_admin"]}})
    assert pure.is_platform_admin is True
    assert pure.is_instance_unrestricted() is False
    assert pure.permissions == set()
    assert pure.permissions_in("tenant-a") == set()
    assert pure.permissions_in("tenant-z") == set()

    # master_admin that is ALSO a viewer in tenant-a gets BOTH surfaces: the
    # control plane, plus exactly its viewer data scope in tenant-a (nothing more).
    both = claims_to_user(
        {
            "sub": "admin-mt",
            "realm_access": {"roles": ["master_admin"]},
            "tenant_roles": {"tenant-a": ["viewer"]},
        }
    )
    assert both.is_platform_admin is True
    assert both.is_instance_unrestricted() is False
    assert both.permissions_in("tenant-a") == {Permission.SEARCH}
    assert both.permissions_in("tenant-z") == set()


def test_flat_claim_back_compat_roles_apply_across_instances():
    """Legacy flat claims (no tenant_roles/groups) behave exactly as before."""
    user = claims_to_user(
        {
            "sub": "u-legacy",
            "realm_access": {"roles": ["content_curator"]},
            "instances": ["tenant-a", "tenant-b"],
        }
    )
    assert user.tenant_roles == {}
    # Flat roles apply uniformly across every claimed instance.
    assert Permission.REVIEW in user.permissions_in("tenant-a")
    assert Permission.REVIEW in user.permissions_in("tenant-b")
    # But not in an instance the caller does not hold.
    assert user.permissions_in("tenant-c") == set()


def test_unknown_group_role_mints_no_membership():
    """Hardening (fix 7): a group path with an UNKNOWN role segment grants nothing.

    A spoofed ``/x/superuser`` group must NOT mint membership in tenant ``x`` nor
    any role; a well-formed ``/tenant-a/admin`` group still works. Regression: the
    old parser accepted ANY ``/a/b`` path as ``{a: {b}}``.
    """
    user = claims_to_user(
        {"sub": "u-spoof-group", "groups": ["/x/superuser", "/tenant-a/admin"]}
    )
    assert user.tenant_roles == {"tenant-a": {"admin"}}
    # No membership in the spoofed tenant.
    assert "x" not in {i.lower() for i in user.instances}
    assert user.permissions_in("x") == set()
    # The known-role group grants exactly its tenant's admin permissions.
    assert user.permissions_in("tenant-a") == set(Permission)


def test_unknown_role_in_tenant_roles_object_is_dropped():
    """An unknown role in the ``tenant_roles`` object is dropped; known ones kept."""
    user = claims_to_user(
        {"sub": "u-mixed", "tenant_roles": {"tenant-a": ["superuser", "viewer"]}}
    )
    assert user.tenant_roles == {"tenant-a": {"viewer"}}
    assert user.permissions_in("tenant-a") == {Permission.SEARCH}


def test_tenant_roles_object_with_only_unknown_role_mints_no_membership():
    user = claims_to_user(
        {"sub": "u-onlyunknown", "tenant_roles": {"tenant-a": ["superuser"]}}
    )
    assert user.tenant_roles == {}
    assert "tenant-a" not in {i.lower() for i in user.instances}
    assert user.permissions_in("tenant-a") == set()


def test_group_prefix_constrains_accepted_paths(monkeypatch):
    """With ``KEYCLOAK_TENANT_GROUP_PREFIX`` set, only paths under it are honoured."""
    monkeypatch.setenv("KEYCLOAK_TENANT_GROUP_PREFIX", "/tenants")
    user = claims_to_user(
        {
            "sub": "u-prefixed",
            "groups": [
                "/tenants/tenant-a/admin",   # under the prefix -> honoured
                "/other/tenant-b/admin",     # outside the prefix -> ignored
                "/tenant-c/viewer",          # no prefix -> ignored
            ],
        }
    )
    assert user.tenant_roles == {"tenant-a": {"admin"}}


def test_group_prefix_unset_is_backcompat():
    """Unset prefix preserves the legacy ``/<instance>/<role>`` parsing."""
    user = claims_to_user({"sub": "u-noprefix", "groups": ["/tenant-a/admin"]})
    assert user.tenant_roles == {"tenant-a": {"admin"}}
    assert user.permissions_in("tenant-a") == set(Permission)


def test_local_bypass_has_all_permissions():
    user = local_bypass_user()
    assert user.token_disabled_mode is True
    assert set(Permission).issubset(user.permissions)


def test_auth_disabled_by_default():
    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        cfg = load_auth_config()
        assert cfg.disabled is True


def test_enabled_auth_requires_keycloak_config_at_startup():
    config = AuthConfig(
        disabled=False,
        keycloak_issuer="",
        keycloak_audience="docs-pipeline-api",
        keycloak_jwks_url="",
    )
    with pytest.raises(RuntimeError, match="KEYCLOAK_ISSUER.*KEYCLOAK_JWKS_URL"):
        validate_auth_config(config)


def test_disabled_auth_does_not_require_keycloak_config():
    config = AuthConfig(
        disabled=True,
        keycloak_issuer="",
        keycloak_audience="docs-pipeline-api",
        keycloak_jwks_url="",
    )
    validate_auth_config(config)


@pytest.mark.asyncio
async def test_get_current_user_bypass_mode():
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/auth/me",
        "headers": [],
        "query_string": b"",
    }
    request = Request(scope)
    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        user = await get_current_user(request)
    assert user.user_id == "local-dev"
    assert user.has_permission(Permission.UPLOAD)


@pytest.mark.asyncio
async def test_require_permission_forbidden():
    checker = require_permission(Permission.ADMIN)
    user = claims_to_user(
        {
            "sub": "u2",
            "realm_access": {"roles": ["viewer"]},
        }
    )
    with pytest.raises(HTTPException) as exc:
        await checker(user=user)
    assert exc.value.status_code == 403


def test_auth_me_endpoint_bypass():
    """Exercise /auth/me without starting Temporal via API lifespan."""
    app = FastAPI()

    @app.get("/auth/me")
    async def auth_me(user=Depends(get_current_user)):
        return {
            "user_id": user.user_id,
            "permissions": sorted(p.value for p in user.permissions),
            "auth_disabled": user.token_disabled_mode,
        }

    with patch.dict(os.environ, {"AUTH_DISABLED": "true"}):
        client = TestClient(app)
        response = client.get("/auth/me")
        body = response.json()
    assert response.status_code == 200, body
    assert body["auth_disabled"] is True
    assert body["user_id"] == "local-dev"
    assert "upload" in body["permissions"]


def test_every_route_is_gated_or_explicitly_classified():
    """Every route must be gated (auth dependency) or on the explicit public allowlist.

    Only ``/health`` is intentionally public (plus framework docs routes). A new
    ungated route fails this test — add the right auth dependency instead of
    widening the allowlist.
    """
    from pipeline.api import app

    # The ONLY intentionally-public application route.
    public = {
        ("GET", "/health"),
    }
    # Framework-provided routes (docs / schema) — not application surfaces.
    framework_paths = {
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }

    def dependency_calls(dependant):
        calls = {dependant.call}
        for child in dependant.dependencies:
            calls.update(dependency_calls(child))
        return calls

    ungated = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        dependant = getattr(route, "dependant", None)
        if not path or dependant is None:
            continue
        if path in framework_paths:
            continue
        has_auth = get_current_user in dependency_calls(dependant)
        for method in methods - {"HEAD", "OPTIONS"}:
            key = (method, path)
            if not has_auth and key not in public:
                ungated.append(f"{method} {path}")

    assert not ungated, (
        "These routes are neither gated nor on the public allowlist "
        f"(only /health may be public): {sorted(ungated)}"
    )


def test_permission_aliases_cover_step2():
    assert Permission.UPLOAD.value == "upload"
    assert Permission.REVIEW.value == "review"
    assert Permission.PIPELINE.value == "pipeline"
    assert Permission.ADMIN.value == "admin"


def test_require_upload_when_auth_enabled_without_token():
    app = FastAPI()

    @app.post("/secure-upload")
    async def secure(user=Depends(require_permission(Permission.UPLOAD))):
        return {"user": user.user_id}

    with patch.dict(
        os.environ,
        {
            "AUTH_DISABLED": "false",
            "KEYCLOAK_ISSUER": "https://example.com/realms/test",
            "KEYCLOAK_JWKS_URL": "https://example.com/realms/test/protocol/openid-connect/certs",
        },
    ):
        client = TestClient(app)
        response = client.post("/secure-upload")
    assert response.status_code == 401
    detail = response.json()["detail"].lower()
    assert "bearer" in detail or "token" in detail


def test_permission_is_str_enum_compatible():
    """Ensure Permission works on Python 3.10 (no StrEnum)."""
    assert isinstance(Permission.UPLOAD, str)
    assert Permission.UPLOAD == "upload"
    assert Permission("upload") is Permission.UPLOAD


def test_decode_and_validate_token_requires_exp_and_rejects_bad_sig():
    from datetime import datetime, timedelta, timezone

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    import jwt as pyjwt

    from pipeline.auth.config import AuthConfig
    from pipeline.auth.jwt import clear_jwks_cache, decode_and_validate_token

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    class _FakeSigningKey:
        def __init__(self, key):
            self.key = key

    class _FakeJwks:
        def get_signing_key_from_jwt(self, _token):
            return _FakeSigningKey(public_key)

    issuer = "https://example.com/realms/test"
    audience = "docs-pipeline-api"
    config = AuthConfig(
        disabled=False,
        keycloak_issuer=issuer,
        keycloak_audience=audience,
        keycloak_jwks_url=f"{issuer}/protocol/openid-connect/certs",
        jwt_leeway_seconds=0,
    )

    clear_jwks_cache()
    with patch("pipeline.auth.jwt._get_jwks_client", return_value=_FakeJwks()):
        # Missing exp must fail.
        no_exp = pyjwt.encode(
            {"sub": "u1", "iss": issuer, "aud": audience, "realm_access": {"roles": ["viewer"]}},
            private_pem,
            algorithm="RS256",
        )
        with pytest.raises(HTTPException) as missing_exp:
            decode_and_validate_token(no_exp, config)
        assert missing_exp.value.status_code == 401

        # Wrong signature must fail.
        other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        bad_sig = pyjwt.encode(
            {
                "sub": "u1",
                "iss": issuer,
                "aud": audience,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
                "realm_access": {"roles": ["viewer"]},
            },
            other_pem,
            algorithm="RS256",
        )
        with pytest.raises(HTTPException) as bad:
            decode_and_validate_token(bad_sig, config)
        assert bad.value.status_code == 401

        # Valid token succeeds.
        good = pyjwt.encode(
            {
                "sub": "u1",
                "preferred_username": "alice",
                "iss": issuer,
                "aud": audience,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
                "realm_access": {"roles": ["viewer"]},
                "instances": ["tenant-a"],
            },
            private_pem,
            algorithm="RS256",
        )
        user = decode_and_validate_token(good, config)
        assert user.user_id == "u1"
        assert user.instances == ["tenant-a"]
        assert Permission.SEARCH in user.permissions
