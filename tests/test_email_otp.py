"""Unit tests for the passwordless email-OTP login helpers.

Keycloak itself is stubbed at the HTTP boundary (``_post``) — these cover the
parts we own: config resolution, email normalisation, and the translation of
Keycloak's error vocabulary into messages a signed-out user can act on.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from pipeline.auth import email_otp

ENV = {
    "KEYCLOAK_ISSUER": "https://kc.example/auth/realms/test",
    "KEYCLOAK_CLIENT_ID": "test-client",
    "KEYCLOAK_CLIENT_SECRET": "s3cret",
}


def _post_returns(status, body):
    return patch.object(email_otp, "_post", return_value=(status, body))


def test_config_falls_back_to_vite_client_id():
    env = {
        "KEYCLOAK_ISSUER": "https://kc.example/auth/realms/test/",
        "VITE_KEYCLOAK_CLIENT_ID": "bharat-vistaar",
        "KEYCLOAK_CLIENT_SECRET": "s3cret",
    }
    with patch.dict("os.environ", env, clear=True):
        cfg = email_otp.load_email_otp_config()
    assert cfg.client_id == "bharat-vistaar"
    # Trailing slash must not survive into the derived URLs.
    assert cfg.send_url == "https://kc.example/auth/realms/test/email-otp/send"
    assert cfg.token_url.endswith("/protocol/openid-connect/token")
    assert cfg.configured


def test_missing_secret_names_the_variable():
    with patch.dict("os.environ", {"KEYCLOAK_ISSUER": "https://kc.example/auth/realms/t"}, clear=True):
        with pytest.raises(HTTPException) as exc:
            email_otp.require_email_otp_config()
    assert exc.value.status_code == 503
    assert "KEYCLOAK_CLIENT_SECRET" in exc.value.detail


@pytest.mark.parametrize("raw", ["  User@Example.COM ", "user@example.com"])
def test_normalize_email_lowercases_and_trims(raw):
    assert email_otp.normalize_email(raw) == "user@example.com"


@pytest.mark.parametrize("raw", ["", "   ", "not-an-email"])
def test_normalize_email_rejects_junk(raw):
    with pytest.raises(HTTPException) as exc:
        email_otp.normalize_email(raw)
    assert exc.value.status_code == 400


def test_send_otp_passes_through_realm_policy():
    body = {
        "status": "ok",
        "message": "If the account exists, a one-time code has been emailed to it",
        "expiresInSeconds": 300,
        "resendAfterSeconds": 30,
        "codeLength": 6,
    }
    with patch.dict("os.environ", ENV, clear=True), _post_returns(200, body):
        result = email_otp.send_otp("user@example.com")
    assert result["resend_after_seconds"] == 30
    assert result["code_length"] == 6


def test_send_otp_reports_broken_smtp_honestly():
    with patch.dict("os.environ", ENV, clear=True), _post_returns(
        503, {"error": "email_delivery_failed"}
    ):
        with pytest.raises(HTTPException) as exc:
            email_otp.send_otp("user@example.com")
    assert exc.value.status_code == 503


def test_verify_returns_the_full_token_set():
    body = {
        "access_token": "at",
        "refresh_token": "rt",
        "id_token": "it",
        "expires_in": 300,
        "token_type": "Bearer",
    }
    with patch.dict("os.environ", ENV, clear=True), _post_returns(200, body):
        tokens = email_otp.verify_otp("user@example.com", "123456")
    assert tokens["access_token"] == "at"
    assert tokens["refresh_token"] == "rt"
    assert tokens["id_token"] == "it"


@pytest.mark.parametrize(
    "description,expected",
    [
        ("The OTP has expired, request a new one", "expired"),
        ("No OTP has been requested for this user, or it was already used", "already used"),
        ("Too many invalid attempts, request a new OTP", "Too many"),
        ("Invalid OTP", "incorrect"),
    ],
)
def test_verify_maps_keycloak_errors_to_actionable_text(description, expected):
    with patch.dict("os.environ", ENV, clear=True), _post_returns(
        401, {"error": "invalid_grant", "error_description": description}
    ):
        with pytest.raises(HTTPException) as exc:
            email_otp.verify_otp("user@example.com", "123456")
    assert exc.value.status_code == 401
    assert expected in exc.value.detail


def test_verify_flags_a_missing_flow_binding_as_misconfiguration():
    # What Keycloak says when the client has no email-otp direct grant override.
    with patch.dict("os.environ", ENV, clear=True), _post_returns(
        400, {"error": "unauthorized_client", "error_description": "Client not allowed"}
    ):
        with pytest.raises(HTTPException) as exc:
            email_otp.verify_otp("user@example.com", "123456")
    assert exc.value.status_code == 503
    assert "email-otp-direct-grant" in exc.value.detail


def test_verify_requires_a_code():
    with patch.dict("os.environ", ENV, clear=True):
        with pytest.raises(HTTPException) as exc:
            email_otp.verify_otp("user@example.com", "   ")
    assert exc.value.status_code == 400


def test_refresh_reports_a_dead_session_as_401():
    with patch.dict("os.environ", ENV, clear=True), _post_returns(
        400, {"error": "invalid_grant", "error_description": "Token is not active"}
    ):
        with pytest.raises(HTTPException) as exc:
            email_otp.refresh_tokens("stale")
    assert exc.value.status_code == 401
