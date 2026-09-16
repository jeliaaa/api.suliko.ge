"""Outbound auth email.

Two things matter here and neither is "does SMTP work". First, sending must
never raise into the request: `POST /auth/password/forgot` answers 204
whatever happens, and an exception escaping the mailer would turn a dead relay
into a 500 that tells an attacker the account exists. Second, the From address
must come from configuration and nowhere else.
"""

from __future__ import annotations

import smtplib
from typing import Any

import pytest

from suliko.api.v1.auth import _reset_email
from suliko.core import mail
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role, User


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Point get_settings() at a mail-configured Settings for one test."""
    from suliko.config import Settings

    base: dict[str, Any] = {
        "smtp_host": "smtp.example.com",
        "smtp_from_email": "noreply@suliko.ge",
        "smtp_from_name": "Suliko",
    }
    base.update(overrides)
    settings = Settings(**base)
    monkeypatch.setattr(mail, "get_settings", lambda: settings)


# ── Failure never escapes ───────────────────────────────────────────────────


async def test_an_unconfigured_mailer_reports_rather_than_raises() -> None:
    """Development has no relay. The message is logged so a reset link can be
    followed out of the log, and the caller still gets a clean answer."""
    result = await mail.send("nino@acme.ge", "Subject", "Body")

    assert result.delivered is False
    assert result.reason == "not_configured"


async def test_an_smtp_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead relay must not become a 500 on the forgot-password path, where
    the status code would leak whether the account exists."""
    _configure(monkeypatch)

    def explode(message: Any) -> None:
        raise smtplib.SMTPServerDisconnected("relay went away")

    monkeypatch.setattr(mail, "_send_blocking", explode)

    result = await mail.send("nino@acme.ge", "Subject", "Body")
    assert result.delivered is False
    assert result.reason == "error"


async def test_a_refused_connection_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """OSError, not SMTPException — connection refused and DNS failure do not
    subclass smtplib's hierarchy, and catching only SMTPException would miss
    the most common outage of all."""
    _configure(monkeypatch)

    def explode(message: Any) -> None:
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(mail, "_send_blocking", explode)

    assert (await mail.send("nino@acme.ge", "S", "B")).reason == "error"


async def test_a_sent_message_is_reported_as_delivered(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    sent: list[Any] = []
    monkeypatch.setattr(mail, "_send_blocking", sent.append)

    result = await mail.send("nino@acme.ge", "Reset your Suliko password", "Body")

    assert result.delivered is True
    assert len(sent) == 1
    assert sent[0]["To"] == "nino@acme.ge"
    assert sent[0]["Subject"] == "Reset your Suliko password"


# ── The From address is configuration ───────────────────────────────────────


def test_the_from_address_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never from user input — a display name taken from a user record would
    let someone register as "Suliko Security" and send plausible mail."""
    _configure(monkeypatch, smtp_from_name="Suliko CRM")

    message = mail._build("nino@acme.ge", "Subject", "Body")
    assert message["From"] == "Suliko CRM <noreply@suliko.ge>"


def test_the_body_is_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """No HTML alternative: an auth mail is three sentences and a link, and a
    second body is a second thing to keep in sync."""
    _configure(monkeypatch)

    message = mail._build("nino@acme.ge", "Subject", "Body")
    assert message.get_content_type() == "text/plain"
    assert not message.is_multipart()


# ── What the reset mail says ────────────────────────────────────────────────


def _people() -> tuple[User, Tenant]:
    user = User(
        id=1,
        tenant_id=1,
        username="nino",
        email="nino@acme.ge",
        full_name="Nino Beridze",
        password_hash="x",
        role=Role.STAFF,
    )
    tenant = Tenant(id=1, slug="acme", display_name="Acme Translations", status=TenantStatus.ACTIVE)
    return user, tenant


def test_the_reset_mail_carries_the_link_and_its_lifetime() -> None:
    user, tenant = _people()
    link = "https://app.suliko.ge/ka/reset-password?token=rst_abc"

    subject, body = _reset_email(user, tenant, link, 60)

    assert "password" in subject.lower()
    assert link in body
    assert "1 hour" in body
    assert tenant.display_name in body
    # Says what to do if it wasn't you, because most recipients of an
    # unexpected reset mail did not ask for it.
    assert "ignore" in body.lower()


def test_the_reset_mail_never_carries_a_password() -> None:
    user, tenant = _people()
    user.password_hash = "$argon2id$v=19$m=65536,t=3,p=4$SECRETHASH"

    _, body = _reset_email(user, tenant, "https://example.test/x", 60)

    assert "SECRETHASH" not in body
    assert "argon2" not in body
