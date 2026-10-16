"""What a bureau is billed by: its order package, and the invoices raised for it.

Not to be confused with ``tenants.plan``, which says what KIND of account a tenant
is (freelancer or bureau, `domain.plans`). A **package** says how many orders a
bureau's monthly fee covers. Every bureau starts on package 1; only a superuser
moves a bureau to another (`api/v1/platform_billing.py`).

Money is whole tetri (``*_cents``), as in the import records: an integer sums and
compares the same on PostgreSQL and on the SQLite the tests use.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Integer, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin, enum_values


class BillingPackage(Base, TimestampMixin):
    """One of the packages a bureau can be on, the same for every bureau.

    A platform table with three rows for bureaus and three for freelancers
    (``audience``), edited by a superuser: how many orders a month the fee covers
    and the fee. When the month's orders reach the package, new orders are refused
    until the account moves up one; nothing is charged per order. Nothing here is
    per account.
    """

    __tablename__ = "billing_packages"

    #: "bureau" or "freelancer" (which also covers a person's own workspace).
    audience: Mapped[str] = mapped_column(
        String(10), primary_key=True, default="bureau", server_default="bureau"
    )
    tier: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    orders_included: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 0 is free. Null: agreed with each account, so there is no price to print and
    #: the account cannot move to it by itself.
    monthly_price_cents: Mapped[int | None] = mapped_column(Integer, default=None)


class BillingInvoiceStatus(enum.StrEnum):
    #: Worked out, not yet asked of the bureau. The bureau does not see it.
    OPEN = "open"
    #: Asked of the bureau, by bank transfer.
    SENT = "sent"
    PAID = "paid"
    #: Not collected, on purpose.
    WAIVED = "waived"


class BillingInvoice(Base, IdMixin, TenantScoped, TimestampMixin):
    """One calendar month of one bureau's fee, as it was worked out.

    Frozen when it is prepared: the package, the count and the amounts are written
    down, so editing a package later does not change what was already asked.
    """

    __tablename__ = "billing_invoices"
    __table_args__ = (UniqueConstraint("tenant_id", "period", name="uq_billing_invoices_period"),)

    #: "2026-10": the calendar month, in the bureau's own time zone.
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    package_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    orders_included: Mapped[int] = mapped_column(Integer, nullable=False)
    orders_counted: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Null: the package has no price (agreed separately).
    plan_fee_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    total_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[BillingInvoiceStatus] = mapped_column(
        Enum(
            BillingInvoiceStatus,
            name="billing_invoice_status",
            values_callable=enum_values,
            native_enum=False,
            length=10,
        ),
        default=BillingInvoiceStatus.OPEN,
        nullable=False,
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    note: Mapped[str | None] = mapped_column(String(500), default=None)
