"""Outbound SMTP for the API.

The ``KC_SMTP_*`` variables are Keycloak's own mail settings; the API borrows
the same relay rather than configuring a second one.

``_send_email`` raises, which is what the login-code flow wants: a code that
never arrives means the login genuinely failed. Callers whose work has already
succeeded by the time they notify someone should use ``try_send_email``, which
reports the outcome instead of failing the request.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl as ssl_lib
from dataclasses import dataclass
from email.message import EmailMessage

from fastapi import HTTPException

# try_send_email outcomes.
SENT = "sent"
NO_RECIPIENT = "no_recipient"
NOT_CONFIGURED = "not_configured"
FAILED = "failed"


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
        logging.error("mailer: failed to send %r to %s: %s", subject, to_addr, exc)
        raise HTTPException(
            503, "Could not send the email right now. Please try again shortly."
        ) from exc


def try_send_email(to_addr: str, subject: str, body: str) -> str:
    """Send an email without ever raising.

    For notifications sent after the real work has already succeeded — a role
    assignment, say. Letting an SMTP failure surface would tell the caller the
    assignment failed when it did not. Returns one of SENT, NO_RECIPIENT,
    NOT_CONFIGURED or FAILED so the caller can report what happened.

    Blocking: run it off the event loop (``asyncio.to_thread``).
    """
    recipient = (to_addr or "").strip()
    if not recipient:
        logging.warning("mailer: no recipient for %r, nothing sent", subject)
        return NO_RECIPIENT

    if not load_smtp_config().configured:
        logging.warning("mailer: KC_SMTP_* not configured, skipped %r", subject)
        return NOT_CONFIGURED

    try:
        _send_email(recipient, subject, body)
    except HTTPException as exc:
        logging.error("mailer: could not send %r to %s: %s", subject, recipient, exc.detail)
        return FAILED
    except Exception:
        logging.exception("mailer: unexpected failure sending %r to %s", subject, recipient)
        return FAILED

    return SENT
