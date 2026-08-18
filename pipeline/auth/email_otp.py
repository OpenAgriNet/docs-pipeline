"""Server-side Keycloak grants for the confidential ``bharat-vistaar`` client.

Both login paths live here because both need the client secret, and both use the
*same* client id. That is forced, not chosen: Keycloak has no "public to the
browser, confidential to the backend" mode, and the ``email-otp`` extension
rejects public clients outright (``invalid_client: Client credentials are
required``). So the only way SSO and email-OTP can share one client is for the
browser to never touch the token endpoint — see ``exchange_authorization_code``.

Email-OTP is two steps, both server-side because they need the client secret:

1. ``POST {issuer}/email-otp/send`` — mails a 6-digit code. Deliberately
   enumeration-safe: the same 200 comes back whether or not the address has an
   account, and a resend inside the cooldown is a silent no-op.
2. ``POST {issuer}/protocol/openid-connect/token`` with ``grant_type=password``,
   ``username=<email>`` and ``otp=<code>`` — returns real Keycloak tokens.

Step 2 goes through the token endpoint rather than ``email-otp/verify`` on
purpose. ``/verify`` only answers ``{"valid": true, ...}`` and *consumes* the
code, so it cannot hand the UI a JWT — and the console's whole RBAC layer reads
the ``groups`` claim out of a real access token. The token endpoint reaches the
same OTP check via the ``email-otp-direct-grant`` flow bound to the client, and
issues access/refresh/id tokens identical in shape to the Google SSO ones.

The client must be confidential with Direct access grants ON and its
*Direct grant flow* override set to ``email-otp-direct-grant`` (Keycloak admin →
Clients → Advanced → Authentication flow overrides).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

# Keycloak answers the first (no-code) token call with this instead of tokens.
# It is a challenge, not a failure — the UI should never see it, since we always
# send a code, but treat it as "your code did not reach us" if it ever surfaces.
_OTP_REQUIRED = "otp_required"

# error_description fragments Keycloak returns for a failed OTP, mapped to text
# a signed-out user can act on. Matched case-insensitively, first hit wins.
_GRANT_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    ("expired", "That code has expired. Request a new one."),
    ("already used", "That code was already used. Request a new one."),
    ("no otp has been requested", "That code is no longer valid. Request a new one."),
    ("too many", "Too many incorrect attempts. Request a new code."),
    ("invalid otp", "That code is incorrect."),
    ("no email address", "This account has no email address configured."),
)


@dataclass(frozen=True)
class EmailOtpConfig:
    """Realm URL plus the confidential client used for both OTP steps."""

    issuer: str
    client_id: str
    client_secret: str

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.client_id and self.client_secret)

    @property
    def send_url(self) -> str:
        return f"{self.issuer}/email-otp/send"

    @property
    def token_url(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/token"


def load_email_otp_config() -> EmailOtpConfig:
    issuer = (os.environ.get("KEYCLOAK_ISSUER") or "").strip().rstrip("/")
    # The OTP client is the same one the browser uses for SSO unless overridden;
    # it just needs a secret here, which the browser never sees.
    client_id = (
        os.environ.get("KEYCLOAK_OTP_CLIENT_ID")
        or os.environ.get("KEYCLOAK_CLIENT_ID")
        or os.environ.get("VITE_KEYCLOAK_CLIENT_ID")
        or ""
    ).strip()
    client_secret = (os.environ.get("KEYCLOAK_CLIENT_SECRET") or "").strip()
    return EmailOtpConfig(issuer=issuer, client_id=client_id, client_secret=client_secret)


def require_email_otp_config() -> EmailOtpConfig:
    cfg = load_email_otp_config()
    if not cfg.configured:
        missing = [
            name
            for name, value in (
                ("KEYCLOAK_ISSUER", cfg.issuer),
                ("KEYCLOAK_CLIENT_ID", cfg.client_id),
                ("KEYCLOAK_CLIENT_SECRET", cfg.client_secret),
            )
            if not value
        ]
        raise HTTPException(
            503,
            "Email OTP login is not configured. Set " + ", ".join(missing) + ".",
        )
    return cfg


def _post(url: str, *, body: Any = None, form: dict | None = None) -> tuple[int, Any]:
    """POST JSON or form-encoded; returns (status, parsed body) and never raises on 4xx."""
    headers: dict[str, str] = {"Accept": "application/json"}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(body or {}).encode()
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, (json.loads(raw.decode()) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"error_description": raw}
    except urllib.error.URLError as exc:
        raise HTTPException(502, f"Keycloak is unreachable: {exc.reason}") from exc


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Enter a valid email address.")
    return email


def send_otp(email: str) -> dict[str, Any]:
    """Mail a login code. The response is identical for unknown accounts by design."""
    cfg = require_email_otp_config()
    status, body = _post(
        cfg.send_url,
        body={
            "email": email,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
        },
    )
    payload = body if isinstance(body, dict) else {}

    if status == 200:
        return {
            "status": "ok",
            # Same wording Keycloak uses — do not make it account-specific.
            "message": payload.get(
                "message", "If the account exists, a one-time code has been emailed to it"
            ),
            "expires_in_seconds": payload.get("expiresInSeconds", 300),
            "resend_after_seconds": payload.get("resendAfterSeconds", 30),
            "code_length": payload.get("codeLength", 6),
        }

    error = str(payload.get("error") or "")
    if error == "email_delivery_failed":
        raise HTTPException(503, "Could not send the email right now. Please try again shortly.")
    if error == "invalid_client":
        # A misconfigured secret is ours to fix, so say so plainly in the log path.
        raise HTTPException(
            503,
            "Email OTP login is misconfigured (Keycloak rejected the client credentials).",
        )
    raise HTTPException(
        502 if status >= 500 else 400,
        str(payload.get("error_description") or "Could not send the login code."),
    )


def _token_response(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "id_token": payload.get("id_token"),
        "token_type": payload.get("token_type", "Bearer"),
        "expires_in": payload.get("expires_in"),
        "refresh_expires_in": payload.get("refresh_expires_in"),
    }


def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    """Renew an OTP session.

    Has to live on the server: the client is confidential, so the browser cannot
    present the secret the token endpoint demands for a refresh_token grant.
    Without this an OTP session would die at the 5-minute access-token expiry.
    """
    cfg = require_email_otp_config()
    token = (refresh_token or "").strip()
    if not token:
        raise HTTPException(400, "Missing refresh token.")

    status, body = _post(
        cfg.token_url,
        form={
            "grant_type": "refresh_token",
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": token,
        },
    )
    payload = body if isinstance(body, dict) else {}

    if status == 200 and payload.get("access_token"):
        return _token_response(payload)

    # An expired or revoked refresh token is a normal end-of-session, not a fault.
    raise HTTPException(401, "Your session has expired. Please sign in again.")


def exchange_authorization_code(
    code: str, redirect_uri: str, code_verifier: str | None = None
) -> dict[str, Any]:
    """Trade an SSO authorization code for tokens, on behalf of the browser.

    The browser cannot run this exchange itself: the client is confidential, so
    the token endpoint answers ``unauthorized_client`` to anyone who cannot
    present the secret. Doing it here is what lets Google SSO and email-OTP
    share a single client id, and it keeps the refresh token off the wire to
    Keycloak from the browser entirely.

    ``code_verifier`` is the PKCE verifier the UI generated before redirecting;
    it is not a secret the server holds, just a value passed through.
    """
    cfg = require_email_otp_config()
    authorization_code = (code or "").strip()
    if not authorization_code:
        raise HTTPException(400, "Missing authorization code.")
    if not (redirect_uri or "").strip():
        raise HTTPException(400, "Missing redirect_uri.")

    form = {
        "grant_type": "authorization_code",
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "code": authorization_code,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        form["code_verifier"] = code_verifier

    status, body = _post(cfg.token_url, form=form)
    payload = body if isinstance(body, dict) else {}

    if status == 200 and payload.get("access_token"):
        return _token_response(payload)

    error = str(payload.get("error") or "")
    description = str(payload.get("error_description") or "")

    if error == "invalid_client" or error == "unauthorized_client":
        raise HTTPException(
            503,
            "SSO is misconfigured (Keycloak rejected the client credentials). Check "
            "KEYCLOAK_CLIENT_ID / KEYCLOAK_CLIENT_SECRET.",
        )
    if error == "invalid_grant":
        # A code is single-use and short-lived; a reload of the callback URL
        # replays a spent one, which is the common way to land here.
        raise HTTPException(401, "That sign-in link has already been used or expired. Try again.")

    raise HTTPException(
        502 if status >= 500 else 400,
        description or "Could not complete sign-in.",
    )


def verify_otp(email: str, code: str) -> dict[str, Any]:
    """Exchange a code for Keycloak tokens via the email-otp direct grant."""
    cfg = require_email_otp_config()
    otp = (code or "").strip()
    if not otp:
        raise HTTPException(400, "Enter the code from your email.")

    status, body = _post(
        cfg.token_url,
        form={
            "grant_type": "password",
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "username": email,
            "otp": otp,
            # Without openid the token works for the API but not /userinfo.
            "scope": "openid",
        },
    )
    payload = body if isinstance(body, dict) else {}

    if status == 200 and payload.get("access_token"):
        return _token_response(payload)

    error = str(payload.get("error") or "")
    description = str(payload.get("error_description") or "")

    if error == _OTP_REQUIRED:
        raise HTTPException(401, "That code is no longer valid. Request a new one.")
    if error == "unauthorized_client":
        raise HTTPException(
            503,
            "Email OTP login is misconfigured: Direct access grants must be enabled on the "
            "Keycloak client, with its Direct grant flow set to 'email-otp-direct-grant'.",
        )
    if error == "invalid_client":
        raise HTTPException(
            503,
            "Email OTP login is misconfigured (Keycloak rejected the client credentials).",
        )

    lowered = description.lower()
    for fragment, message in _GRANT_ERROR_HINTS:
        if fragment in lowered:
            raise HTTPException(401, message)

    if status == 401:
        # Covers "Invalid user credentials", which is what an unknown email looks
        # like here. Stay vague — this endpoint is unauthenticated.
        raise HTTPException(401, "That code is incorrect or has expired.")
    raise HTTPException(
        502 if status >= 500 else 400,
        description or "Could not verify the login code.",
    )
