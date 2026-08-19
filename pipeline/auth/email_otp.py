"""Server-side email-OTP login plus Keycloak SSO code exchange for the
confidential ``knowledge-engine`` client.

Both login paths live here because both need the client secret, and both use
the *same* client id. That is forced, not chosen: Keycloak has no "public to
the browser, confidential to the backend" mode, so the browser can never touch
the token endpoint directly — see ``exchange_authorization_code``.

Email-OTP is fully custom, not delegated to Keycloak's email-authenticator
plugin: that plugin registers only browser-flow SPIs (no RealmResourceProvider
REST endpoint), and its Email OTP authenticator can't identify a user without
a preceding password step — it's a second-factor plugin, not a standalone
passwordless one. Neither fact is fixable from config, so the API owns the
whole OTP lifecycle instead:

1. ``send_otp`` generates a code, stores its hash (see ``pipeline.db``), and
   emails it directly via SMTP (``KC_SMTP_*`` — the same creds the Keycloak
   container uses, already present in every service's env via ``env_file``).
2. ``verify_otp`` checks the code, then bridges to a real Keycloak session:
   the Admin API resets the user's Keycloak password to a random one-time
   secret, and a plain password grant (client's Direct Grant Flow must be
   Keycloak's *default* — no OTP override — since correctness was already
   checked here) mints access/refresh/id tokens identical in shape to the
   Google SSO ones.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import smtplib
import ssl as ssl_lib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

from fastapi import HTTPException

from . import keycloak_admin
from .. import db

OTP_LENGTH = 6
OTP_TTL_SECONDS = 300
OTP_RESEND_COOLDOWN_SECONDS = 30
OTP_MAX_ATTEMPTS = 5
# Approximates a typical SMTP round-trip, so the "no such account" path takes
# about as long as the "account exists, email sent" path.
_FAKE_SEND_DELAY_SECONDS = 0.35


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


# --------------------------------------------------------------------------
# SMTP — sends the OTP code directly, independent of Keycloak entirely.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    from_addr: str
    from_name: str
    use_auth: bool
    use_starttls: bool
    use_ssl: bool

    @property
    def configured(self) -> bool:
        return bool(self.host and self.from_addr)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_smtp_config() -> SmtpConfig:
    return SmtpConfig(
        host=(os.environ.get("KC_SMTP_HOST") or "").strip(),
        port=int(os.environ.get("KC_SMTP_PORT") or 587),
        username=(os.environ.get("KC_SMTP_USERNAME") or "").strip(),
        password=os.environ.get("KC_SMTP_PASSWORD") or "",
        from_addr=(os.environ.get("KC_SMTP_FROM") or "").strip(),
        from_name=(os.environ.get("KC_SMTP_FROM_DISPLAY_NAME") or "Bharat Vistaar").strip(),
        use_auth=_env_bool("KC_SMTP_AUTH", True),
        use_starttls=_env_bool("KC_SMTP_STARTTLS", True),
        use_ssl=_env_bool("KC_SMTP_SSL", False),
    )


def _send_email(to_addr: str, subject: str, body: str) -> None:
    cfg = load_smtp_config()
    if not cfg.configured:
        raise HTTPException(503, "Email sending is not configured (KC_SMTP_* missing).")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{cfg.from_name} <{cfg.from_addr}>" if cfg.from_name else cfg.from_addr
    message["To"] = to_addr
    message.set_content(body)

    try:
        if cfg.use_ssl:
            with smtplib.SMTP_SSL(
                cfg.host, cfg.port, timeout=20, context=ssl_lib.create_default_context()
            ) as smtp:
                if cfg.use_auth:
                    smtp.login(cfg.username, cfg.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=20) as smtp:
                if cfg.use_starttls:
                    smtp.starttls(context=ssl_lib.create_default_context())
                if cfg.use_auth:
                    smtp.login(cfg.username, cfg.password)
                smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        logging.error("email-otp: failed to send code to %s: %s", to_addr, exc)
        raise HTTPException(
            503, "Could not send the email right now. Please try again shortly."
        ) from exc


# --------------------------------------------------------------------------
# OTP generation / verification
# --------------------------------------------------------------------------


def _generate_code() -> str:
    return f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"


def _otp_pepper() -> bytes:
    """Purpose-specific subkey derived from the confidential client secret.

    A 6-digit code is only 1M possibilities, so hashing it with a public salt
    (the email) and a fast, unkeyed hash would let anyone with read access to
    the email_otps table brute-force a live code offline in milliseconds. HMAC
    with a server-only key closes that: cracking a stored hash now also
    requires the key, not just DB access. Derived (not reused directly) to
    keep this key separate from the client secret's actual purpose.
    """
    client_secret = require_email_otp_config().client_secret
    return hmac.new(client_secret.encode(), b"email-otp-code-hash-v1", hashlib.sha256).digest()


def _hash_code(email: str, code: str) -> str:
    # Keyed with the email too: a leaked hash can't be replayed against a
    # different account, and two users with the same code hash differently.
    return hmac.new(_otp_pepper(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def _find_keycloak_user(email: str) -> dict[str, Any] | None:
    cfg = keycloak_admin.require_admin_config()
    token = keycloak_admin._admin_token(cfg)
    admin = keycloak_admin._admin_root(cfg)
    status, found = keycloak_admin._req(
        "GET",
        f"{admin}/users?{urllib.parse.urlencode({'email': email, 'exact': 'true'})}",
        token=token,
    )
    if isinstance(found, list) and found:
        return found[0]
    return None


def send_otp(email: str) -> dict[str, Any]:
    """Mail a login code. Enumeration-safe: identical response either way.

    Only accounts that actually exist in Keycloak get a real email sent — this
    both avoids using the API's SMTP relay to spam arbitrary addresses, and
    means a mistyped/unknown email produces no visible difference in the
    response the caller sees.
    """
    now = datetime.now(timezone.utc)
    generic_response = {
        "status": "ok",
        "message": "If the account exists, a one-time code has been emailed to it",
        "resend_after_seconds": OTP_RESEND_COOLDOWN_SECONDS,
    }

    # A cooldown row is written on every request below regardless of account
    # existence, so this branch (and the countdown it reveals) fires
    # identically for real and unknown addresses — it must never be gated on
    # whether an account was actually found, or the countdown becomes a
    # response oracle for account existence.
    existing = db.get_email_otp(email)
    if existing is not None:
        last_sent = datetime.fromisoformat(existing["last_sent_at"])
        elapsed = (now - last_sent).total_seconds()
        if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
            # Cooldown — silent no-op, but still looks identical to a fresh send.
            remaining = max(0, int(OTP_RESEND_COOLDOWN_SECONDS - elapsed))
            return {**generic_response, "resend_after_seconds": remaining}

    # Always do the same lookup + storage work whether or not the account
    # exists, so neither the response nor its timing can be used to enumerate
    # which emails have accounts.
    user = _find_keycloak_user(email)
    expires_at = now + timedelta(seconds=OTP_TTL_SECONDS)
    if user is not None:
        code = _generate_code()
        code_hash = _hash_code(email, code)
    else:
        # Unreachable by any real 6-digit code — verify_otp always reports
        # "incorrect" for this address, same as a genuine wrong guess.
        code = None
        code_hash = secrets.token_hex(32)

    db.store_email_otp(
        email=email,
        code_hash=code_hash,
        expires_at=expires_at.isoformat(),
        created_at=now.isoformat(),
        last_sent_at=now.isoformat(),
    )

    if user is not None:
        try:
            _send_email(
                email,
                "Your sign-in code",
                f"Your one-time sign-in code is: {code}\n\n"
                f"This code expires in {OTP_TTL_SECONDS // 60} minutes. "
                "If you did not request this, you can safely ignore this email.",
            )
            logging.info("email-otp: login code sent to %s", email)
        except HTTPException:
            # Never let an SMTP failure surface as a different response/status
            # than the "no such account" path — that would itself be an
            # enumeration oracle. The stored code is now simply unreachable;
            # the user's next resend attempt (after the cooldown) tries again.
            logging.error("email-otp: send failed for %s, response withheld", email)
    else:
        # Matches the typical SMTP round-trip _send_email would otherwise
        # take, so response latency doesn't leak account existence either.
        time.sleep(_FAKE_SEND_DELAY_SECONDS)

    return generic_response


def verify_otp(email: str, code: str) -> dict[str, Any]:
    """Check a code and, if correct, mint real Keycloak tokens for the account."""
    otp = (code or "").strip()
    if not otp:
        raise HTTPException(400, "Enter the code from your email.")

    row = db.get_email_otp(email)
    if row is None:
        raise HTTPException(401, "That code is no longer valid. Request a new one.")

    now = datetime.now(timezone.utc)
    if now > datetime.fromisoformat(row["expires_at"]):
        db.delete_email_otp(email)
        raise HTTPException(401, "That code has expired. Request a new one.")

    if row["attempts"] >= OTP_MAX_ATTEMPTS:
        db.delete_email_otp(email)
        raise HTTPException(401, "Too many incorrect attempts. Request a new code.")

    if row["code_hash"] != _hash_code(email, otp):
        db.increment_email_otp_attempts(email)
        raise HTTPException(401, "That code is incorrect.")

    db.delete_email_otp(email)
    return _mint_tokens_for_verified_email(email)


def _mint_tokens_for_verified_email(email: str) -> dict[str, Any]:
    """Bridge a verified OTP to a real Keycloak session.

    Resets the user's Keycloak password to a random one-time secret, spends
    it on a plain password grant, then immediately rotates it again — so the
    password this account holds is never a value that stays valid or
    memorable, it exists as a real credential only for the instant it's
    used.

    IMPORTANT residual exposure: this relies on the knowledge-engine client's
    Direct Grant Flow being Keycloak's unmodified default (no OTP step), so
    that this plain password grant succeeds. That also means *any* caller who
    knows the client secret and a user's *current* Keycloak password can get
    a token straight from Keycloak's token endpoint, bypassing this API (and
    its rate limiting / enumeration protections) entirely — the bridge only
    adds a path, it doesn't remove the plain one. The immediate rotation here
    keeps that window as small as possible for passwords *this code* sets,
    but does not protect against a password set some other way (e.g. by an
    admin, or during testing). A fully closed version of this would need a
    second Keycloak client — direct-grant-only, no browser flows, ideally
    reachable only from the internal network — dedicated solely to this
    bridge, so the bypass surface doesn't overlap with the client the browser
    also uses for SSO.
    """
    user = _find_keycloak_user(email)
    if user is None:
        # Shouldn't happen — send_otp only emails codes to real accounts —
        # but the account could have been deleted in between.
        raise HTTPException(401, "No account found for this email.")

    admin_cfg = keycloak_admin.require_admin_config()
    admin_token = keycloak_admin._admin_token(admin_cfg)
    admin_root = keycloak_admin._admin_root(admin_cfg)

    def _rotate_password() -> str:
        new_password = secrets.token_urlsafe(32)
        keycloak_admin._req(
            "PUT",
            f"{admin_root}/users/{user['id']}/reset-password",
            token=admin_token,
            body={"type": "password", "value": new_password, "temporary": False},
        )
        return new_password

    ephemeral_password = _rotate_password()

    cfg = require_email_otp_config()
    status, body = _post(
        cfg.token_url,
        form={
            "grant_type": "password",
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "username": email,
            "password": ephemeral_password,
            "scope": "openid",
        },
    )
    payload = body if isinstance(body, dict) else {}

    # Rotate again regardless of outcome — the just-used password must not
    # remain a live credential either way.
    try:
        _rotate_password()
    except HTTPException:
        logging.error("email-otp: post-grant password rotation failed for %s", email)

    if status == 200 and payload.get("access_token"):
        return _token_response(payload)

    logging.error(
        "email-otp: password-grant bridge failed for %s: status=%s body=%s",
        email, status, payload,
    )
    raise HTTPException(502, "Could not complete sign-in. Please try again.")


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
    """Renew an OTP or SSO session.

    Has to live on the server: the client is confidential, so the browser cannot
    present the secret the token endpoint demands for a refresh_token grant.
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
