"""Per-tenant credentials for the external services the CRM talks to.

Ports the Integrations tab of the PHP app's `settings.php`, which stored each
provider's keys in its own table and several of them — notably
`api24_tokens.api24_password` — in plain text. That is the first thing this
changes.

## One row per provider, not one column per field

The PHP has `misc_integration_settings` with a column per key, plus separate
tables for BOG and API24, and adding a provider means a migration. Here a
provider is a row: `provider` names it, `config` holds the non-secret fields as
JSON, and `secrets` holds the sensitive ones as a single encrypted blob. Adding
a provider is a row, not a schema change.

## What counts as a secret

Anything that grants access if it leaks: API keys, client secrets, passwords,
service-account private keys. Those go in `secrets`, encrypted per tenant with
the envelope scheme in `suliko.core.crypto`, and are **never returned by the
API** — not masked, not partially. The UI shows whether a secret is set and
when it changed, and offers to replace it.

Non-secret configuration — an SMS sender name, a source IBAN, a reCAPTCHA
*site* key, which is public by design — lives in `config` as plain JSON so it
can be displayed, searched and diffed in the audit log.

## Why encrypted rather than a secrets manager

A secrets manager is the better answer and this is not a substitute for one.
But these credentials are entered by bureau staff through a web form, per
tenant, at arbitrary times — there is no deploy step to inject them at. Storing
them encrypted under a per-tenant key derived from a master key held outside
the database means a stolen database dump is not a stolen set of API keys.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin, enum_values


class IntegrationProvider(enum.StrEnum):
    """The external services a bureau can connect.

    Values are stable identifiers — they appear in the audit log and in the
    API path. Renaming one is a migration, not a rename.
    """

    #: Google Drive service account. Per-order folders, document storage.
    GOOGLE_DRIVE = "google_drive"
    #: Bank of Georgia e-commerce: client payment links, status, refunds.
    BOG_ECOMMERCE = "bog_ecommerce"
    #: Bank of Georgia Business Online: outbound transfers to translators.
    BOG_BUSINESS = "bog_business"
    #: SmsOffice — the Georgian SMS gateway the PHP app uses.
    SMS_OFFICE = "sms_office"
    #: Outbound email (SMTP) for confirmations and document delivery.
    SMTP = "smtp"
    #: ElevenLabs speech-to-text.
    ELEVENLABS = "elevenlabs"
    #: reCAPTCHA on public forms.
    RECAPTCHA = "recaptcha"
    #: content.api24.ge — AI document translation.
    API24 = "api24"


class IntegrationCredential(Base, IdMixin, TenantScoped, TimestampMixin):
    """One provider's settings for one tenant."""

    __tablename__ = "integration_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_integration_tenant_provider"),
    )

    provider: Mapped[IntegrationProvider] = mapped_column(
        Enum(
            IntegrationProvider,
            name="integration_provider",
            values_callable=enum_values,
            # VARCHAR + CHECK rather than a native enum: adding a provider is
            # then an ALTER of one constraint, not a type migration.
            native_enum=False,
            length=40,
        ),
        nullable=False,
    )

    #: Off by default. A provider with credentials saved but not enabled is a
    #: deliberate state — staff set it up before go-live, or suspend it during
    #: an incident without destroying the keys.
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: Non-secret fields, shape defined per provider by the API schemas.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    #: The secret fields, as one encrypted JSON object. Null when none is set
    #: yet. Never decrypted for display — only to make an outbound call.
    secrets: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)

    #: When the secret was last replaced, so the UI can say "set 3 months ago"
    #: without ever reading the value.
    secrets_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    secrets_updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    #: Result of the last "Test connection", so the screen can show whether
    #: these credentials have ever actually worked.
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    #: Why the last check failed. Written by us, never the raw provider body,
    #: which can echo the credential back.
    last_check_detail: Mapped[str | None] = mapped_column(String(500), default=None)

    @property
    def has_secrets(self) -> bool:
        return self.secrets is not None
