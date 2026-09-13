"""The three payment ledgers, allocations, and expenses.

The shape here is deliberately the PHP's, because the PHP's shape is right:

- A payment is one real-world money movement (one bank transfer, one card
  charge). It is NOT tied to a single order.
- An allocation says how much of that payment applies to a given order. One
  payment, N allocations — this is how a client settling six invoices with one
  transfer is represented.
- "Amount paid" on an order is therefore always SUM(allocations), never a
  stored column. A stored column is a second source of truth that drifts the
  first time someone edits an allocation.

Notary allocations key on ``order_document_id``, not ``order_id``, because
``notary_cost`` lives on the document. Do not "simplify" that.
"""

from __future__ import annotations

import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin, enum_values


class PaymentMethod(enum.StrEnum):
    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    CARD = "card"
    BOG_ONLINE = "bog_online"
    OTHER = "other"


class ClientPayment(Base, IdMixin, TenantScoped, TimestampMixin):
    """Money in, from a client."""

    __tablename__ = "client_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_client_payments_tenant_client", "tenant_id", "client_id"),
        Index("ix_client_payments_tenant_date", "tenant_id", "payment_date"),
    )

    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(
            PaymentMethod,
            name="payment_method",
            values_callable=enum_values,
            native_enum=False,
            length=30,
        ),
        default=PaymentMethod.BANK_TRANSFER,
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(String(255), default=None)
    recorded_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    # Deduplicates a double-submitted payment form or a retried request.
    # Unique per tenant; see docs/03-SECURITY-AND-TENANCY.md §5.1 rule 9.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), default=None)


class ClientPaymentAllocation(Base, IdMixin, TenantScoped, TimestampMixin):
    __tablename__ = "client_payment_allocations"
    __table_args__ = (
        CheckConstraint("amount_allocated > 0", name="allocated_positive"),
        Index("ix_cpa_tenant_order", "tenant_id", "order_id"),
        Index("ix_cpa_tenant_payment", "tenant_id", "payment_id"),
    )

    payment_id: Mapped[int] = mapped_column(
        ForeignKey("client_payments.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    amount_allocated: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)


class ClientRefund(Base, IdMixin, TenantScoped, TimestampMixin):
    __tablename__ = "client_refunds"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_client_refunds_tenant_payment", "tenant_id", "payment_id"),
    )

    payment_id: Mapped[int] = mapped_column(
        ForeignKey("client_payments.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    refund_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), default=None)
    recorded_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64), default=None)


class TranslatorPayment(Base, IdMixin, TenantScoped, TimestampMixin):
    """Money out, to a translator."""

    __tablename__ = "translator_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_translator_payments_tenant_translator", "tenant_id", "translator_id"),
        Index("ix_translator_payments_tenant_date", "tenant_id", "payment_date"),
    )

    translator_id: Mapped[int] = mapped_column(
        ForeignKey("translators.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(
            PaymentMethod,
            name="payment_method",
            values_callable=enum_values,
            native_enum=False,
            length=30,
        ),
        default=PaymentMethod.BANK_TRANSFER,
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(String(255), default=None)
    recorded_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64), default=None)


class TranslatorPaymentAllocation(Base, IdMixin, TenantScoped, TimestampMixin):
    __tablename__ = "translator_payment_allocations"
    __table_args__ = (
        CheckConstraint("amount_allocated > 0", name="allocated_positive"),
        Index("ix_tpa_tenant_order", "tenant_id", "order_id"),
        Index("ix_tpa_tenant_payment", "tenant_id", "payment_id"),
    )

    payment_id: Mapped[int] = mapped_column(
        ForeignKey("translator_payments.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    amount_allocated: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)


class NotaryPayment(Base, IdMixin, TenantScoped, TimestampMixin):
    """Money out, to a notary — or reimbursed through a translator.

    ``paid_via_translator_payment_id`` covers the common real-world case: the
    translator pays the notary office in cash and is reimbursed in the same
    bank transfer as their translation fee. Who paid the notary is a
    payment-time fact, not an order-time one, which is why it lives here and
    not on the document.
    """

    __tablename__ = "notary_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_notary_payments_tenant_date", "tenant_id", "payment_date"),
        Index("ix_notary_payments_tenant_via", "tenant_id", "paid_via_translator_payment_id"),
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(
        Enum(
            PaymentMethod,
            name="payment_method",
            values_callable=enum_values,
            native_enum=False,
            length=30,
        ),
        default=PaymentMethod.BANK_TRANSFER,
        nullable=False,
    )
    paid_via_translator_payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("translator_payments.id", ondelete="SET NULL"), default=None
    )
    notes: Mapped[str | None] = mapped_column(String(255), default=None)
    recorded_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64), default=None)


class NotaryPaymentAllocation(Base, IdMixin, TenantScoped, TimestampMixin):
    """Keyed on the DOCUMENT, because notary_cost lives on the document."""

    __tablename__ = "notary_payment_allocations"
    __table_args__ = (
        CheckConstraint("amount_allocated > 0", name="allocated_positive"),
        Index("ix_npa_tenant_document", "tenant_id", "order_document_id"),
        Index("ix_npa_tenant_payment", "tenant_id", "payment_id"),
    )

    payment_id: Mapped[int] = mapped_column(
        ForeignKey("notary_payments.id", ondelete="CASCADE"), nullable=False
    )
    order_document_id: Mapped[int] = mapped_column(
        ForeignKey("order_documents.id", ondelete="RESTRICT"), nullable=False
    )
    amount_allocated: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)


class Expense(Base, IdMixin, TenantScoped, TimestampMixin):
    """A business expense.

    ``order_id`` null means a general expense (rent, software). Non-null ties
    it to one order — a courier fee, a notary office charge — and it is then
    subtracted from that order's profit.
    """

    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_expenses_tenant_date", "tenant_id", "expense_date"),
        Index("ix_expenses_tenant_order", "tenant_id", "order_id"),
    )

    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), default=None
    )
    expense_date: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    recorded_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
