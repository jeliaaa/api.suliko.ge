"""Business mail: what a bureau sends its own clients and translators.

Documents, today. Auth mail (password resets, invites) is `core/mail.py` and
stays apart on purpose; the two differ in three ways.

## Who it comes from

A bureau that has filled in its own SMTP server under Settings, Integrations
sends from its own address, through its own server. Everyone else, a
freelancer above all, sends through the platform's relay with no setup at
all: from the platform's address, under the organisation's name, with
Reply-To set to the organisation's own address so an answer reaches a person
and not a no-reply box. `resolve_sender` makes that choice.

## Failure is told

`core.mail.send` never raises, because its caller must not reveal whether an
account exists. Here the person pressed Send and is watching: a message that
did not leave has to say so, or they will believe the client has the file.
`deliver` raises `MailDeliveryError`.

## Attachments

The message carries files. They are read into memory to be encoded, so the
caller bounds their total size first (`Settings.document_mail_max_bytes`).

Plain text bodies only, as in `core/mail.py`, and for the same reasons.
"""

from __future__ import annotations

import asyncio
import json
import re
import smtplib
from dataclasses import dataclass
from email.headerregistry import Address
from email.message import EmailMessage
from typing import Any, Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.config import get_settings
from suliko.core.crypto import DecryptionError, decrypt_for_tenant
from suliko.core.errors import UpstreamUnavailableError
from suliko.models.integration import IntegrationCredential, IntegrationProvider

log = structlog.get_logger()

Via = Literal["smtp", "platform"]

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_ADDRESS = re.compile(r"^[^@\s<>\"]+@[^@\s<>\"]+\.[^@\s<>\"]+$")


class MailNotConfiguredError(UpstreamUnavailableError):
    """Neither the bureau nor the platform has a mail server to send through."""

    error_code = "mail_not_configured"


class MailDeliveryError(UpstreamUnavailableError):
    """The mail server refused the message, or could not be reached."""

    error_code = "mail_delivery_failed"


@dataclass(frozen=True, slots=True)
class Attachment:
    file_name: str
    content_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class Sender:
    """One way out: a server, and who the message says it is from."""

    #: "smtp" is the bureau's own server, "platform" is Suliko's.
    via: Via
    host: str
    port: int
    username: str | None
    password: str | None
    starttls: bool
    ssl: bool
    from_email: str
    from_name: str
    reply_to: str | None
    timeout: int


def is_address(value: str | None) -> bool:
    """Good enough to hand to an SMTP server, which has the last word."""
    return value is not None and _ADDRESS.fullmatch(value.strip()) is not None


def _display_name(value: str, limit: int = 80) -> str:
    """A name fit for a From header: one line, no control characters."""
    return _CONTROL_CHARACTERS.sub(" ", value).strip()[:limit]


def _truthy(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _own_smtp(
    row: IntegrationCredential, tenant_id: int, *, organisation: str, reply_to: str | None
) -> Sender | None:
    """The bureau's own server, if every part needed to use it is there."""
    config = row.config or {}
    secrets: dict[str, Any] = {}
    if row.secrets is not None:
        try:
            secrets = dict(json.loads(decrypt_for_tenant(tenant_id, row.secrets)))
        except (DecryptionError, ValueError):
            # The master key changed since this was saved. The Integrations
            # screen says so; here it simply means "not usable".
            log.warning("business_mail_smtp_secrets_unreadable", tenant_id=tenant_id)
            return None

    host = str(config.get("host", "")).strip()
    from_email = str(config.get("from_email", "")).strip()
    if not host or not is_address(from_email):
        return None
    try:
        port = int(str(config.get("port", "")).strip() or 587)
    except ValueError:
        return None

    # "Use STARTTLS" off means the server wants TLS from the first byte (465).
    starttls = _truthy(config.get("use_tls"), default=True)
    return Sender(
        via="smtp",
        host=host,
        port=port,
        username=str(config.get("username", "")).strip() or None,
        password=str(secrets.get("password", "")) or None,
        starttls=starttls,
        ssl=not starttls,
        from_email=from_email,
        from_name=_display_name(str(config.get("from_name", "")).strip() or organisation),
        reply_to=reply_to,
        timeout=get_settings().smtp_timeout_seconds,
    )


def _platform(*, organisation: str, reply_to: str | None) -> Sender | None:
    settings = get_settings()
    if not settings.email_configured:
        return None
    return Sender(
        via="platform",
        host=settings.smtp_host or "",
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password.get_secret_value() or None,
        starttls=settings.smtp_starttls,
        ssl=settings.smtp_ssl,
        from_email=settings.smtp_from_email or "",
        # The address is the platform's, so the name says whose message it
        # is AND that it came through Suliko: a recipient can tell it is not
        # the bureau's own mailbox, and so can a spam filter's reader.
        from_name=_display_name(f"{organisation} via {settings.smtp_from_name}"),
        reply_to=reply_to,
        timeout=settings.smtp_timeout_seconds,
    )


async def resolve_sender(
    db: AsyncSession, tenant_id: int, *, organisation: str, reply_to: str | None
) -> Sender | None:
    """How this bureau's mail leaves: its own server if set up, else Suliko's.

    None when there is no way out at all (development with no relay).
    `reply_to` is used either way; pass None when the bureau has no address
    of its own to answer to.
    """
    reply = reply_to.strip() if reply_to and is_address(reply_to) else None
    row = (
        await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.provider == IntegrationProvider.SMTP,
                IntegrationCredential.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        own = _own_smtp(row, tenant_id, organisation=organisation, reply_to=reply)
        if own is not None:
            return own
    return _platform(organisation=organisation, reply_to=reply)


def build_message(
    sender: Sender,
    *,
    to: str,
    subject: str,
    body: str,
    attachments: list[Attachment],
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = _CONTROL_CHARACTERS.sub(" ", subject).strip()
    message["To"] = to.strip()
    local, _, domain = sender.from_email.partition("@")
    message["From"] = str(Address(sender.from_name, local, domain))
    if sender.reply_to:
        message["Reply-To"] = sender.reply_to
    message.set_content(body)
    for attachment in attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        message.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.file_name,
        )
    return message


def _send_blocking(sender: Sender, message: EmailMessage) -> None:
    client: smtplib.SMTP | smtplib.SMTP_SSL
    if sender.ssl:
        client = smtplib.SMTP_SSL(sender.host, sender.port, timeout=sender.timeout)
    else:
        client = smtplib.SMTP(sender.host, sender.port, timeout=sender.timeout)

    with client:
        if sender.starttls and not sender.ssl:
            client.starttls()
        if sender.username and sender.password:
            client.login(sender.username, sender.password)
        client.send_message(message)


async def deliver(sender: Sender, message: EmailMessage) -> None:
    """Send it, or raise `MailDeliveryError`. The server's own words are
    logged, not returned: they can name internal hosts and accounts."""
    try:
        await asyncio.to_thread(_send_blocking, sender, message)
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("business_mail_failed", via=sender.via, host=sender.host, error=str(exc))
        if sender.via == "smtp":
            raise MailDeliveryError(
                "Your mail server did not accept the message. Check the Email (SMTP) "
                "settings under Settings, Integrations."
            ) from exc
        raise MailDeliveryError("The message could not be sent right now. Try again.") from exc
    log.info("business_mail_sent", via=sender.via)


__all__ = [
    "Attachment",
    "MailDeliveryError",
    "MailNotConfiguredError",
    "Sender",
    "Via",
    "build_message",
    "deliver",
    "is_address",
    "resolve_sender",
]
