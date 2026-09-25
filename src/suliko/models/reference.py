"""Per-tenant reference data: languages, document types, pricing, company."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Boolean, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin


class Language(Base, IdMixin, TenantScoped, TimestampMixin):
    """Per tenant, because bureaus differ in which languages they offer."""

    __tablename__ = "languages"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_languages_tenant_code"),)

    code: Mapped[str] = mapped_column(String(5), nullable=False)
    name_en: Mapped[str] = mapped_column(String(100), nullable=False)
    name_ka: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class DocumentType(Base, IdMixin, TenantScoped, TimestampMixin):
    """A document category and its price multiplier.

    ``price_multiplier`` is a direct factor in the pricing formula — see
    ``suliko.domain.pricing``.
    """

    __tablename__ = "document_types"
    __table_args__ = (Index("ix_document_types_tenant_name", "tenant_id", "name_en"),)

    name_en: Mapped[str] = mapped_column(String(255), nullable=False)
    name_ka: Mapped[str] = mapped_column(String(255), nullable=False)
    price_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), default=Decimal("1.0"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LanguagePairPrice(Base, IdMixin, TenantScoped, TimestampMixin):
    """Base rate per page for a directed language pair."""

    __tablename__ = "language_pair_prices"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_language",
            "target_language",
            name="uq_lpp_tenant_pair",
        ),
        Index("ix_lpp_tenant_active", "tenant_id", "is_active"),
    )

    source_language: Mapped[str] = mapped_column(String(5), nullable=False)
    target_language: Mapped[str] = mapped_column(String(5), nullable=False)
    price_per_page: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    @property
    def pair(self) -> str:
        return f"{self.source_language}-{self.target_language}"


class Company(Base, IdMixin, TenantScoped, TimestampMixin):
    """The tenant's own legal identity, as printed on invoices and acts.

    The PHP has exactly two global rows (``legal_entity`` and ``brand``). Here
    it is per-tenant, which is the whole point of the B2B pivot: each partner
    bureau invoices under its own name and bank details.
    """

    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("tenant_id", "role", name="uq_companies_tenant_role"),)

    # 'legal_entity' = invoicing company | 'brand' = public-facing company
    role: Mapped[str] = mapped_column(String(50), nullable=False)

    name_ka: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    name_en: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    director_ka: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    director_en: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    site: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    address_ka: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    address_en: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    id_number: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    email: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    phone: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    whatsapp: Mapped[str] = mapped_column(String(50), default="", nullable=False)


class CompanyBankAccount(Base, IdMixin, TenantScoped, TimestampMixin):
    """A company may hold several; ``is_primary`` is the one used on invoices."""

    __tablename__ = "company_bank_accounts"
    __table_args__ = (Index("ix_cba_tenant_company", "tenant_id", "company_id"),)

    company_id: Mapped[int] = mapped_column(nullable=False)
    bank_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    bank_iban: Mapped[str] = mapped_column(String(34), default="", nullable=False)
    bank_swift: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    account_name: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class TenantSettings(Base, IdMixin, TenantScoped, TimestampMixin):
    """One row per tenant. General settings from the Settings → General tab."""

    __tablename__ = "tenant_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_tenant_settings_tenant"),)

    default_language: Mapped[str] = mapped_column(String(5), default="ka", nullable=False)
    system_email: Mapped[str | None] = mapped_column(String(255), default=None)

    # Pricing knobs the PHP keeps in config_shared.php. Per-tenant here so a
    # partner bureau can set its own express/urgent surcharges and courier fee
    # without a code change.
    urgency_multiplier_standard: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), default=Decimal("1.0"), nullable=False
    )
    urgency_multiplier_express: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), default=Decimal("1.5"), nullable=False
    )
    urgency_multiplier_urgent: Mapped[Decimal] = mapped_column(
        Numeric(6, 3), default=Decimal("2.0"), nullable=False
    )
    delivery_fee: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("10"), nullable=False
    )
    default_translator_share: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), default=Decimal("0.5"), nullable=False
    )

    # Days from the order date to the default due date, per urgency. The PHP
    # hard-codes these in its client portal and partner API (same day, +2,
    # +5); here they pre-fill the order form and stay editable per order.
    # `server_default` as well as `default`: see adding-a-column-migration —
    # a fresh database (0001, from metadata) and a migrated one (0008) must
    # end up with the same DDL.
    due_days_standard: Mapped[int] = mapped_column(
        Integer, default=5, server_default="5", nullable=False
    )
    due_days_express: Mapped[int] = mapped_column(
        Integer, default=2, server_default="2", nullable=False
    )
    due_days_urgent: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
