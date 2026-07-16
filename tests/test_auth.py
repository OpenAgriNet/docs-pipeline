"""Unit tests for auth plumbing (permissions, bypass mode, guards)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from pipeline.auth.config import load_auth_config
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


def test_mutate_routes_declare_auth_dependency():
    """Guarded write routes should require a permission dependency in the signature."""
    import inspect

    from pipeline.api import app

    guarded = {
        ("POST", "/upload"),
        ("POST", "/documents"),
        ("POST", "/documents/batch"),
        ("DELETE", "/documents/{workflow_id}"),
        ("POST", "/documents/{workflow_id}/restore"),
        ("POST", "/documents/{workflow_id}/reingest"),
        ("POST", "/documents/{workflow_id}/approve-ocr"),
        ("POST", "/documents/{workflow_id}/approve-chunks"),
        ("POST", "/documents/bulk/reindex"),
        ("PUT", "/settings/search"),
        ("POST", "/admin/index/create"),
        ("PATCH", "/documents/{workflow_id}/pages/{page_num}"),
    }

    found = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        endpoint = getattr(route, "endpoint", None)
        if not path or endpoint is None:
            continue
        for method in methods:
            key = (method, path)
            if key not in guarded:
                continue
            sig = inspect.signature(endpoint)
            has_auth = any(
                "Require" in str(param.annotation) or "AuthUser" in str(param.annotation)
                for param in sig.parameters.values()
            )
            assert has_auth, f"Missing auth dependency on {method} {path}"
            found.add(key)

    assert found == guarded


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
