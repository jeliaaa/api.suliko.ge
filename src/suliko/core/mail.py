"""Outbound auth email.

Password resets today; invites and welcome mail next. Business mail — client
confirmations, document delivery — does NOT come through here: that goes out
through the tenant's own SMTP integration, because it has to carry the
bureau's address. See `Settings.smtp_host` for why auth mail is the exception.

## Why stdlib and a thread

`smtplib` is synchronous, so each send runs in a worker thread via
`asyncio.to_thread`. The alternative is another dependency (`aiosmtplib`) for
a call that happens a few times a day per tenant and is already bounded by
`smtp_timeout_seconds`. A thread is the cheaper answer.

## Why sending never raises

`send` returns a result instead of raising. The one caller that matters —
`POST /auth/password/forgot` — must answer 204 identically whether the
account exists, whether the mail was accepted, and whether the relay is down.
Letting an SMTP error surface would turn the response time or the status code
into an account-enumeration oracle, which is the exact thing that endpoint is
shaped to avoid.
"""

from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass
from email.headerregistry import Address
from email.message import EmailMessage

import structlog

from suliko.config import get_settings

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class MailResult:
    """What happened, for logging and tests. Never for the HTTP response."""

    delivered: bool
    #: "sent", "not_configured", or "error". Stable enough to assert on.
    reason: str


def _build(to: str, subject: str, body: str) -> EmailMessage:
    settings = get_settings()
    message = EmailMessage()
    message["Subject"] = subject
    message["To"] = to
    # The From address is configuration, never user input — a display name
    # taken from a user would let someone forge a plausible-looking sender.
    from_email = settings.smtp_from_email or "noreply@localhost"
    local, _, domain = from_email.partition("@")
    message["From"] = str(Address(settings.smtp_from_name, local, domain))
    # Plain text only, deliberately. An auth mail is three sentences and a
    # link; HTML buys nothing and costs a second body to keep in sync, plus a
    # well-known phishing-lookalike surface.
    message.set_content(body)
    return message


def _send_blocking(message: EmailMessage) -> None:
    settings = get_settings()
    host = settings.smtp_host or ""
    timeout = settings.smtp_timeout_seconds

    client: smtplib.SMTP | smtplib.SMTP_SSL
    if settings.smtp_ssl:
        client = smtplib.SMTP_SSL(host, settings.smtp_port, timeout=timeout)
    else:
        client = smtplib.SMTP(host, settings.smtp_port, timeout=timeout)

    with client:
        if settings.smtp_starttls and not settings.smtp_ssl:
            client.starttls()
        username = settings.smtp_username
        password = settings.smtp_password.get_secret_value()
        if username and password:
            client.login(username, password)
        client.send_message(message)


async def send(to: str, subject: str, body: str) -> MailResult:
    """Deliver one message. Never raises.

    With no SMTP configured the message is logged instead of sent, so local
    development and tests can follow a reset link out of the log rather than
    needing a relay. Production refuses to start in that state — see
    `Settings.validate_for_production`.
    """
    settings = get_settings()

    if not settings.email_configured:
        log.warning(
            "email_not_configured",
            to=to,
            subject=subject,
            # The body carries a single-use reset link. That is acceptable in
            # a log only because this branch cannot happen in production.
            body=body if not settings.is_production else "<suppressed>",
        )
        return MailResult(delivered=False, reason="not_configured")

    message = _build(to, subject, body)
    try:
        await asyncio.to_thread(_send_blocking, message)
    except (smtplib.SMTPException, OSError):
        # OSError covers connection refused, DNS failure and timeouts.
        log.exception("email_send_failed", to=to, subject=subject)
        return MailResult(delivered=False, reason="error")

    log.info("email_sent", to=to, subject=subject)
    return MailResult(delivered=True, reason="sent")


__all__ = ["MailResult", "send"]
