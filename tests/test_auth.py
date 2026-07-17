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
    assert Permission.MANAGE_USERS in permissions_for_roles(["master_admin"])
    assert permissions_for_roles(["viewer"]) == {Permission.SEARCH}
    assert permissions_for_roles(["unknown-role"]) == set()


def test_claims_to_user_maps_keycloak_shape():
    user = claims_to_user(
        {
            "sub": "user-1",
            "preferred_username": "aayush",
            "email": "aayush@example.com",
            "realm_access": {"roles": ["content_curator"]},
            "instances": ["amul", "bv"],
            "envs": ["dev"],
        }
    )
    assert user.user_id == "user-1"
    assert user.username == "aayush"
    assert Permission.UPLOAD in user.permissions
    assert user.instances == ["amul", "bv"]
    assert user.envs == ["dev"]
    assert user.has_instance("Amul")
    assert not user.has_env("prod")


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
    """New routes must choose auth, public access, or tracked transition debt."""
    from pipeline.api import app

    public = {
        ("GET", "/openapi.json"),
        ("GET", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("GET", "/redoc"),
        ("GET", "/health"),
        ("GET", "/taxonomy/domain-tags"),
        ("GET", "/pipeline/stages"),
    }

    # These existing reads remain open only while AUTH_DISABLED=true. Keep this
    # list explicit and delete entries as Phase 1 adds auth + tenant scoping.
    transition_reads = {
        ("GET", "/operations/queue"),
        ("GET", "/runs"),
        ("GET", "/runs/{job_id}"),
        ("GET", "/documents/{workflow_id}/error-details"),
        ("GET", "/documents/{workflow_id}/runtime"),
        ("GET", "/documents/{workflow_id}/artifacts"),
        ("GET", "/documents/{workflow_id}/artifacts/{artifact_id}"),
        ("GET", "/documents/{workflow_id}/artifacts/{artifact_id}/content"),
        ("GET", "/documents/{workflow_id}/jobs"),
        ("GET", "/documents/{workflow_id}/stage-io"),
        ("GET", "/documents/{workflow_id}/allowed-actions"),
        ("GET", "/documents/{workflow_id}/graph"),
        ("GET", "/audit"),
        ("GET", "/documents/{workflow_id}/audit"),
        ("GET", "/documents/{workflow_id}/pages"),
        ("GET", "/documents/{workflow_id}/pages/{page_num}"),
        ("GET", "/chunks/search"),
        ("GET", "/documents/{workflow_id}/chunks"),
        ("GET", "/documents/{workflow_id}/chunks/{chunk_num}"),
        ("GET", "/documents/{workflow_id}/export/markdown"),
        ("GET", "/documents/{workflow_id}/export/chunks"),
        ("GET", "/documents/{workflow_id}/pdf"),
        ("GET", "/provenance/chunk"),
        ("GET", "/documents/{workflow_id}/marqo"),
        ("GET", "/documents/{workflow_id}/marqo/chunks"),
        ("GET", "/marqo/indexes/{index_name}/settings"),
        ("GET", "/marqo/indexes/{index_name}/stats"),
        ("GET", "/marqo/indexes/summary"),
        ("POST", "/marqo/search"),
        ("GET", "/admin/index/schema"),
        ("GET", "/admin/ingest-info"),
        ("GET", "/settings/search"),
        ("GET", "/settings/search/audit"),
    }

    def dependency_calls(dependant):
        calls = {dependant.call}
        for child in dependant.dependencies:
            calls.update(dependency_calls(child))
        return calls

    classified = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        dependant = getattr(route, "dependant", None)
        if not path or dependant is None:
            continue
        has_auth = get_current_user in dependency_calls(dependant)
        for method in methods - {"HEAD", "OPTIONS"}:
            key = (method, path)
            assert has_auth or key in public or key in transition_reads, (
                f"Route must add auth or be explicitly classified: {method} {path}"
            )
            classified.add(key)

    assert transition_reads <= classified


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
                "instances": ["amul"],
            },
            private_pem,
            algorithm="RS256",
        )
        user = decode_and_validate_token(good, config)
        assert user.user_id == "u1"
        assert user.instances == ["amul"]
        assert Permission.SEARCH in user.permissions
