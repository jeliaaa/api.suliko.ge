"""Clients, translators, notaries — the three directories."""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Enum, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin, enum_values


class ClientType(enum.StrEnum):
    B2B = "B2B"
    B2C = "B2C"


class Client(Base, IdMixin, TenantScoped, TimestampMixin):
    __tablename__ = "clients"
    __table_args__ = (
        Index("ix_clients_tenant_name", "tenant_id", "name"),
        Index("ix_clients_tenant_type", "tenant_id", "client_type"),
        Index("ix_clients_tenant_email", "tenant_id", "email"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_type: Mapped[ClientType] = mapped_column(
        Enum(
            ClientType,
            name="client_type",
            values_callable=enum_values,
            native_enum=False,
            length=10,
        ),
        default=ClientType.B2C,
        nullable=False,
    )
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    phone: Mapped[str | None] = mapped_column(String(50), default=None)
    address: Mapped[str | None] = mapped_column(String(500), default=None)

    # National ID / company registration number. Sensitive: masked in list
    # responses, full value only behind an audited reveal.
    # See docs/03-SECURITY-AND-TENANCY.md §8.
    personal_number: Mapped[str | None] = mapped_column(String(50), default=None)

    # Free text in the PHP app, not an enum — kept that way deliberately so
    # existing values migrate without a lossy mapping.
    acquisition_source: Mapped[str | None] = mapped_column(String(255), default=None)

    notes: Mapped[str | None] = mapped_column(Text, default=None)

    # Client-portal credentials (phase 11). Null for clients who never sign in.
    portal_password_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    def __repr__(self) -> str:
        return f"<Client {self.id} {self.name!r} t{self.tenant_id}>"


class Translator(Base, IdMixin, TenantScoped, TimestampMixin):
    __tablename__ = "translators"
    __table_args__ = (
        Index("ix_translators_tenant_name", "tenant_id", "name"),
        Index("ix_translators_tenant_active", "tenant_id", "is_active"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50), default=None)
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    office_address: Mapped[str | None] = mapped_column(String(255), default=None)
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    experience_from: Mapped[date | None] = mapped_column(Date, default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Default per-page rate paid to this translator. Per-pair overrides live in
    # TranslatorLanguagePair.
    default_rate: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)

    # Translator-portal credentials. Presence of a username is what the UI
    # renders as the "Active" / "None" account badge.
    portal_username: Mapped[str | None] = mapped_column(String(100), default=None)
    portal_password_hash: Mapped[str | None] = mapped_column(String(255), default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    bank_iban: Mapped[str | None] = mapped_column(String(34), default=None)
    bank_inn: Mapped[str | None] = mapped_column(String(20), default=None)
    bank_code: Mapped[str | None] = mapped_column(String(20), default=None)

    google_drive_folder: Mapped[str | None] = mapped_column(String(100), default=None)

    @property
    def has_portal_account(self) -> bool:
        return bool(self.portal_username and self.portal_password_hash)


class TranslatorLanguagePair(Base, IdMixin, TenantScoped, TimestampMixin):
    """A directed pair this translator works in, with an optional own rate.

    The PHP stores this as a comma-separated ``languages`` string on the
    translator row, which cannot be queried ("find me an EN->KA translator"
    becomes a LIKE scan) and cannot carry a per-pair rate. Normalising it is
    one of the deliberate deviations in the build docs.
    """

    __tablename__ = "translator_language_pairs"
    __table_args__ = (
        Index("ix_tlp_tenant_translator", "tenant_id", "translator_id"),
        Index("ix_tlp_tenant_pair", "tenant_id", "source_language", "target_language"),
    )

    translator_id: Mapped[int] = mapped_column(nullable=False)
    source_language: Mapped[str] = mapped_column(String(5), nullable=False)
    target_language: Mapped[str] = mapped_column(String(5), nullable=False)
    rate_per_page: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), default=None)


class Notary(Base, IdMixin, TenantScoped, TimestampMixin):
    """Admin-managed identity and bank details only.

    Notaries deliberately have no portal login — they do no AI translation and
    nobody has asked for notary self-service. Don't add one without a reason.
    """

    __tablename__ = "notaries"
    __table_args__ = (Index("ix_notaries_tenant_name", "tenant_id", "name"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50), default=None)
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    registration_number: Mapped[str | None] = mapped_column(String(100), default=None)
    office_address: Mapped[str | None] = mapped_column(String(255), default=None)
    comment: Mapped[str | None] = mapped_column(Text, default=None)

    bank_iban: Mapped[str | None] = mapped_column(String(34), default=None)
    bank_inn: Mapped[str | None] = mapped_column(String(20), default=None)
    bank_code: Mapped[str | None] = mapped_column(String(20), default=None)

    @property
    def bank_ready(self) -> bool:
        """Drives the `Ready` / `No IBAN` badge in the notary list."""
        return bool(self.bank_iban)
