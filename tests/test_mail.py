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

from suliko.api.v1.auth import _reset_email, _verification_email
from suliko.core import mail
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Account, Role, User


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
    _configure(monkeypatch, smtp_from_name="Suliko Office")

    message = mail._build("nino@acme.ge", "Subject", "Body")
    assert message["From"] == "Suliko Office <noreply@suliko.ge>"


def test_a_message_without_html_is_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    message = mail._build("nino@acme.ge", "Subject", "Body")
    assert message.get_content_type() == "text/plain"
    assert not message.is_multipart()


def test_a_message_with_html_carries_both_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plain text comes first and stays: a reader that cannot show HTML, and
    a spam filter comparing the two, both get the same words."""
    _configure(monkeypatch)

    message = mail._build("nino@acme.ge", "Subject", "Plain body", "<p>Html body</p>")

    assert message.get_content_type() == "multipart/alternative"
    plain, html = message.iter_parts()
    assert plain.get_content_type() == "text/plain"
    assert "Plain body" in plain.get_content()
    assert html.get_content_type() == "text/html"
    assert "Html body" in html.get_content()


def test_a_line_break_in_the_subject_cannot_inject_a_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The organisation's name is in the subject, and an organisation's name is
    typed by a person."""
    _configure(monkeypatch)

    message = mail._build("nino@acme.ge", "Acme\r\nBcc: someone@else.test", "Body")

    assert message["Bcc"] is None
    assert "\n" not in message["Subject"]


async def test_the_html_reaches_the_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)
    sent: list[Any] = []
    monkeypatch.setattr(mail, "_send_blocking", sent.append)

    await mail.send("nino@acme.ge", "Subject", "Plain", "<p>Html</p>")

    assert sent[0].get_body(("html",)).get_content().strip() == "<p>Html</p>"


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


def _account() -> Account:
    return Account(
        email="nino@acme.ge",
        full_name="Nino Beridze",
        password_hash="$argon2id$v=19$m=65536,t=3,p=4$SECRETHASH",
    )


def test_the_reset_mail_carries_the_link_and_its_lifetime() -> None:
    account = _account()
    link = "https://app.suliko.ge/ka/reset-password?token=rst_abc"

    message = _reset_email(account, link, 60)
    subject, body = message.subject, message.text

    assert "password" in subject.lower()
    assert link in body
    assert "1 hour" in body
    assert account.email in body
    # One password for every organisation — the mail says so.
    assert "every organisation" in body
    # Says what to do if it wasn't you, because most recipients of an
    # unexpected reset mail did not ask for it.
    assert "ignore" in body.lower()


def test_the_reset_mail_never_carries_a_password() -> None:
    message = _reset_email(_account(), "https://example.test/x", 60)

    for version in (message.text, message.html):
        assert "SECRETHASH" not in version
        assert "argon2" not in version


# ── What the verification mail says ─────────────────────────────────────────


def test_the_verification_mail_carries_the_link_and_its_lifetime() -> None:
    user, tenant = _people()
    link = "https://app.suliko.ge/ka/verify-email?token=rst_abc"

    message = _verification_email(user, tenant, link, 72)
    subject, body = message.subject, message.text

    assert "confirm" in subject.lower()
    assert link in body
    assert "3 days" in body
    assert tenant.display_name in body


def test_the_verification_mail_says_nothing_is_blocked_on_it() -> None:
    """Unlike a reset link, this one gates nothing — the account already
    works. The mail should not read like a warning."""
    user, tenant = _people()

    body = _verification_email(user, tenant, "https://example.test/x", 72).text

    assert "on hold" in body.lower() or "not on hold" in body.lower()


def test_the_verification_mail_never_carries_a_password() -> None:
    user, tenant = _people()
    user.password_hash = "$argon2id$v=19$m=65536,t=3,p=4$SECRETHASH"

    message = _verification_email(user, tenant, "https://example.test/x", 72)

    for version in (message.text, message.html):
        assert "SECRETHASH" not in version
        assert "argon2" not in version


def test_verification_ttl_under_a_day_is_shown_in_hours() -> None:
    user, tenant = _people()
    body = _verification_email(user, tenant, "https://example.test/x", 6).text
    assert "6 hours" in body
