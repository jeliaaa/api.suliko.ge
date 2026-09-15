"""Order aggregate queries.

The list and detail screens need figures that live across four tables —
documents, status events, payment allocations and expenses. Computing them
per row would be one query per order; these build reusable subqueries instead,
so a 20-row page is a constant number of round trips.

## A note on tenancy

These select columns rather than ORM entities, so the `with_loader_criteria`
filter in `db/tenancy.py` does NOT apply to them. That is safe for two
independent reasons, and it is worth knowing both:

1. They are always joined to `orders`, which IS entity-filtered, and order ids
   are globally unique — so another tenant's rows cannot join in.
2. PostgreSQL row-level security filters them anyway. The GUC is set per
   transaction, so it applies to column selects and raw SQL alike.

Point 2 is the one that makes this genuinely safe rather than incidentally
safe. It is exactly why layer 3 exists.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.sql import Subquery

from suliko.models.finance import ClientPaymentAllocation, Expense
from suliko.models.order import Order, OrderDocument, OrderStatusEvent


def latest_status_subquery() -> Subquery:
    """The current status of every order.

    `DISTINCT ON` rather than a correlated subquery: PostgreSQL resolves it
    with one index scan instead of re-running a lookup per row. The status
    history is append-only, so "current" always means "most recent event".
    """
    return (
        select(
            OrderStatusEvent.order_id.label("order_id"),
            OrderStatusEvent.status.label("status"),
            OrderStatusEvent.changed_at.label("changed_at"),
        )
        .distinct(OrderStatusEvent.order_id)
        .order_by(OrderStatusEvent.order_id, OrderStatusEvent.changed_at.desc())
        .subquery("latest_status")
    )


def document_totals_subquery() -> Subquery:
    """Per-order document aggregates.

    `gross_profit` here is BEFORE order expenses. Expenses are subtracted
    separately because joining both at once multiplies them by the document
    count — the exact bug the PHP's profit query is shaped to avoid
    (docs/02-PRODUCT-SPEC.md §6.2).
    """
    return (
        select(
            OrderDocument.order_id.label("order_id"),
            func.coalesce(func.sum(OrderDocument.price), 0).label("documents_total"),
            func.coalesce(func.sum(OrderDocument.translator_cost), 0).label("translator_total"),
            func.coalesce(func.sum(OrderDocument.notary_cost), 0).label("notary_total"),
            func.coalesce(
                func.sum(
                    OrderDocument.price - OrderDocument.translator_cost - OrderDocument.notary_cost
                ),
                0,
            ).label("gross_profit"),
            func.count().label("document_count"),
            func.coalesce(func.sum(OrderDocument.page_count), 0).label("page_count"),
        )
        .group_by(OrderDocument.order_id)
        .subquery("document_totals")
    )


def paid_subquery() -> Subquery:
    """How much has been allocated to each order.

    Always derived, never stored. A payment covers several orders at once, and
    an "amount paid" column would be a second source of truth that drifts the
    first time an allocation is edited.
    """
    return (
        select(
            ClientPaymentAllocation.order_id.label("order_id"),
            func.coalesce(func.sum(ClientPaymentAllocation.amount_allocated), 0).label("paid"),
        )
        .group_by(ClientPaymentAllocation.order_id)
        .subquery("paid_totals")
    )


def order_expenses_subquery() -> Subquery:
    """Expenses tied to a specific order.

    `order_id IS NULL` means a general business expense (rent, software) and is
    excluded here — it belongs to the company, not to any job.
    """
    return (
        select(
            Expense.order_id.label("order_id"),
            func.coalesce(func.sum(Expense.amount), 0).label("expenses"),
        )
        .where(Expense.order_id.is_not(None))
        .group_by(Expense.order_id)
        .subquery("order_expenses")
    )


def base_order_query() -> tuple[Select[Any], Subquery, Subquery, Subquery, Subquery]:
    """An order query with every aggregate attached.

    Returns the statement plus the subqueries, so callers can filter and sort
    on the aggregated columns without rebuilding them.

    All four joins are LEFT: a brand-new order has no documents, no payments
    and no expenses, and must still appear in the list.
    """
    status = latest_status_subquery()
    docs = document_totals_subquery()
    paid = paid_subquery()
    expenses = order_expenses_subquery()

    stmt = (
        select(
            Order,
            status.c.status,
            docs.c.documents_total,
            docs.c.translator_total,
            docs.c.notary_total,
            docs.c.gross_profit,
            docs.c.document_count,
            docs.c.page_count,
            paid.c.paid,
            expenses.c.expenses,
        )
        .outerjoin(status, status.c.order_id == Order.id)
        .outerjoin(docs, docs.c.order_id == Order.id)
        .outerjoin(paid, paid.c.order_id == Order.id)
        .outerjoin(expenses, expenses.c.order_id == Order.id)
    )

    return stmt, status, docs, paid, expenses
