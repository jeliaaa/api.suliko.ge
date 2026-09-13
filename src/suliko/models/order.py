"""Orders (the PHP's "translations"), their documents, and status history."""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin, enum_values


class Urgency(enum.StrEnum):
    STANDARD = "standard"
    EXPRESS = "express"
    URGENT = "urgent"


class HandoverMethod(enum.StrEnum):
    SCAN = "scan"
    PICKUP = "pickup"
    DELIVERY = "delivery"


class CopyType(enum.StrEnum):
    ORIGINAL = "original"
    PLAIN = "plain"
    NOTARY_ORIGINAL = "notary_original"
    NOTARY_COPY = "notary_copy"
    NOTARY_CERTIFIED = "notary_certified"

    @property
    def is_notarized(self) -> bool:
        return self in (
            CopyType.NOTARY_ORIGINAL,
            CopyType.NOTARY_COPY,
            CopyType.NOTARY_CERTIFIED,
        )


class Order(Base, IdMixin, TenantScoped, TimestampMixin):
    """A customer order, split into one or more documents.

    Money is ``Numeric``, never float. The PHP computes totals in PHP floats,
    which drift on sums of many lines; the Python equivalent would be worse
    because of how often we aggregate.

    There is no ``status`` column. The current status is the latest row in
    ``order_status_events`` — an append-only log, exactly as the PHP does it.
    A denormalised column would be a second source of truth that eventually
    disagrees with the history.
    """

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_tenant_date", "tenant_id", "order_date"),
        Index("ix_orders_tenant_client", "tenant_id", "client_id"),
        Index("ix_orders_tenant_due", "tenant_id", "due_date"),
    )

    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )

    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, default=None)

    contact_info: Mapped[str | None] = mapped_column(String(255), default=None)

    urgency: Mapped[Urgency] = mapped_column(
        Enum(Urgency, name="urgency", values_callable=enum_values, native_enum=False, length=20),
        default=Urgency.STANDARD,
        nullable=False,
    )
    handover_method: Mapped[HandoverMethod] = mapped_column(
        Enum(
            HandoverMethod,
            name="handover_method",
            values_callable=enum_values,
            native_enum=False,
            length=20,
        ),
        default=HandoverMethod.SCAN,
        nullable=False,
    )
    delivery_address: Mapped[str | None] = mapped_column(String(500), default=None)
    delivery_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0"), nullable=False
    )

    # How the order arrived: 'office', 'website', 'api', 'phone'.
    source: Mapped[str | None] = mapped_column(String(50), default=None)

    google_drive_folder: Mapped[str | None] = mapped_column(String(100), default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class OrderDocument(Base, IdMixin, TenantScoped, TimestampMixin):
    """One document within an order — the unit that is priced and assigned.

    Costs are stored, not recomputed on read. A rate change must never
    retroactively alter what an existing order charged, and staff routinely
    hand-adjust ``translator_cost`` and ``notary_cost`` after the fact.
    """

    __tablename__ = "order_documents"
    __table_args__ = (
        Index("ix_order_documents_tenant_order", "tenant_id", "order_id"),
        Index("ix_order_documents_tenant_translator", "tenant_id", "translator_id"),
        Index("ix_order_documents_tenant_notary", "tenant_id", "notary_id"),
    )

    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    document_type_id: Mapped[int] = mapped_column(
        ForeignKey("document_types.id", ondelete="RESTRICT"), nullable=False
    )

    source_language: Mapped[str] = mapped_column(String(5), nullable=False)
    target_language: Mapped[str] = mapped_column(String(5), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    copy_type: Mapped[CopyType] = mapped_column(
        Enum(CopyType, name="copy_type", values_callable=enum_values, native_enum=False, length=30),
        default=CopyType.ORIGINAL,
        nullable=False,
    )
    is_notarized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # What the client is charged for this document (translation + notary).
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # What we owe the translator for it.
    translator_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0"), nullable=False
    )
    # What the notary charges for it.
    notary_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0"), nullable=False
    )

    # Nullable, unlike the PHP's `translator_id = 1` sentinel meaning
    # "unassigned" — a magic row that breaks referential integrity and makes
    # "unassigned" indistinguishable from "assigned to translator #1".
    translator_id: Mapped[int | None] = mapped_column(
        ForeignKey("translators.id", ondelete="SET NULL"), default=None
    )
    # Only meaningful once notarized. Null = the fee was fronted by the office
    # or by the translator, which the Finances screen reports as
    # "Unattributed Notary Fees".
    notary_id: Mapped[int | None] = mapped_column(
        ForeignKey("notaries.id", ondelete="SET NULL"), default=None
    )

    @property
    def profit(self) -> Decimal:
        return self.price - self.translator_cost - self.notary_cost


class OrderStatusEvent(Base, IdMixin, TenantScoped, TimestampMixin):
    """Append-only status history.

    Status values are stored verbatim as the PHP wrote them, including the
    ``payed`` misspelling and the space-separated values ("sent to the
    translator"). They are live production data; normalising them rewrites
    history. See suliko.domain.statuses.
    """

    __tablename__ = "order_status_events"
    __table_args__ = (Index("ix_ose_tenant_order_time", "tenant_id", "order_id", "changed_at"),)

    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(60), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    changed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    note: Mapped[str | None] = mapped_column(String(500), default=None)
