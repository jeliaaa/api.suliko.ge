"""Dashboard and report aggregates.

One router, because the dashboard cards and the reports charts are the same
figures over different windows.

## The profit shape

Profit is computed per order and then summed, never as one flat join. Joining
documents and expenses together multiplies the expense by the document count —
the bug the PHP's query is deliberately shaped around
(docs/02-PRODUCT-SPEC.md §6.2).

## Cancelled orders

Excluded from every figure here. A cancelled job was never revenue, and
including it makes a bad month look like a good one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, and_, case, func, select

from suliko.api.deps import Db, require
from suliko.domain.orders import (
    document_totals_subquery,
    latest_status_subquery,
    order_expenses_subquery,
    paid_subquery,
)
from suliko.domain.statuses import CLOSED_STATUSES, EXCLUDED_FROM_AGGREGATES
from suliko.models.directory import Client, ClientType
from suliko.models.order import Order
from suliko.security.permissions import Permission

router = APIRouter(prefix="/reports", tags=["reports"])


class PeriodTotals(BaseModel):
    orders: int
    revenue: Decimal
    profit: Decimal
    #: profit / revenue, or null when there is no revenue to divide by.
    margin: float | None


class Comparison(BaseModel):
    """Like-for-like month-over-month.

    `previous_partial` covers the SAME number of days of last month, not the
    whole of it. Comparing five days against thirty makes every early-month
    figure look catastrophic, which is the mistake this shape exists to avoid.
    `previous_full` is shown alongside for context.
    """

    days_elapsed: int
    current: PeriodTotals
    previous_partial: PeriodTotals
    previous_full: PeriodTotals
    #: Ratio vs previous_partial. Null when last month's partial was zero —
    #: "up from nothing" is not a percentage.
    revenue_change: float | None
    profit_change: float | None
    orders_change: float | None


class StatusCount(BaseModel):
    status: str
    count: int


class DashboardSummary(BaseModel):
    all_time: PeriodTotals
    #: Owed to us across unpaid and part-paid orders.
    awaiting_payment: Decimal
    awaiting_payment_orders: int
    this_month: Comparison
    by_status: list[StatusCount]
    open_orders: int
    overdue_orders: int


class MonthlyPoint(BaseModel):
    month: str
    revenue: Decimal
    profit: Decimal
    orders: int


class CostBreakdown(BaseModel):
    translator: Decimal
    notary: Decimal
    delivery: Decimal
    expenses: Decimal


class ByClientType(BaseModel):
    client_type: ClientType
    revenue: Decimal
    profit: Decimal
    orders: int


class ReportSummary(BaseModel):
    period: PeriodTotals
    monthly_trends: list[MonthlyPoint]
    cost_breakdown: CostBreakdown
    by_client_type: list[ByClientType]


def _totals_query(start: date | None = None, end: date | None = None) -> Select[Any]:
    """Revenue, profit and order count over a date window.

    Built as a per-order subquery that is then aggregated, so the expense
    subtraction happens once per order rather than once per document.
    """
    status = latest_status_subquery()
    docs = document_totals_subquery()
    expenses = order_expenses_subquery()

    per_order = (
        select(
            Order.id.label("order_id"),
            (func.coalesce(docs.c.documents_total, 0) + Order.delivery_cost).label("revenue"),
            (
                func.coalesce(docs.c.gross_profit, 0)
                + Order.delivery_cost
                - func.coalesce(expenses.c.expenses, 0)
            ).label("profit"),
        )
        .outerjoin(status, status.c.order_id == Order.id)
        .outerjoin(docs, docs.c.order_id == Order.id)
        .outerjoin(expenses, expenses.c.order_id == Order.id)
        .where(func.coalesce(status.c.status, "").notin_(tuple(EXCLUDED_FROM_AGGREGATES)))
    )

    if start is not None:
        per_order = per_order.where(Order.order_date >= start)
    if end is not None:
        per_order = per_order.where(Order.order_date <= end)

    sub = per_order.subquery()
    return select(
        func.count().label("orders"),
        func.coalesce(func.sum(sub.c.revenue), 0).label("revenue"),
        func.coalesce(func.sum(sub.c.profit), 0).label("profit"),
    ).select_from(sub)


async def _totals(db: Db, start: date | None = None, end: date | None = None) -> PeriodTotals:
    row = (await db.execute(_totals_query(start, end))).one()
    orders, revenue, profit = int(row[0] or 0), Decimal(row[1] or 0), Decimal(row[2] or 0)
    return PeriodTotals(
        orders=orders,
        revenue=revenue,
        profit=profit,
        margin=float(profit / revenue) if revenue else None,
    )


def _change(current: Decimal | int, previous: Decimal | int) -> float | None:
    """Ratio of change, or None when there is no baseline.

    Returning None rather than 0 or infinity: "up 100% from nothing" is
    meaningless, and the UI should omit the figure instead of printing one.
    """
    if not previous:
        return None
    return float((Decimal(current) - Decimal(previous)) / Decimal(previous))


@router.get("/dashboard", response_model=DashboardSummary)
async def dashboard(
    db: Db,
    _: Annotated[object, Depends(require(Permission.REPORTS_READ))],
    today: date | None = None,
) -> DashboardSummary:
    # `today` is injectable so the month-boundary arithmetic is testable
    # without freezing the clock. UTC because the server's local timezone is
    # an accident of where it happens to be hosted.
    today = today or datetime.now(UTC).date()

    month_start = today.replace(day=1)
    days_elapsed = (today - month_start).days + 1

    previous_month_end = month_start - timedelta(days=1)
    previous_month_start = previous_month_end.replace(day=1)
    # Clamped: comparing 31 March-to-date against February would run past the
    # end of the month and silently include March days.
    previous_partial_end = min(
        previous_month_start + timedelta(days=days_elapsed - 1), previous_month_end
    )

    all_time = await _totals(db)
    current = await _totals(db, month_start, today)
    previous_partial = await _totals(db, previous_month_start, previous_partial_end)
    previous_full = await _totals(db, previous_month_start, previous_month_end)

    # Outstanding: what is owed across orders that are not fully paid.
    status = latest_status_subquery()
    docs = document_totals_subquery()
    paid = paid_subquery()

    owed = (
        select(
            Order.id.label("order_id"),
            (
                func.coalesce(docs.c.documents_total, 0)
                + Order.delivery_cost
                - func.coalesce(paid.c.paid, 0)
            ).label("owed"),
        )
        .outerjoin(status, status.c.order_id == Order.id)
        .outerjoin(docs, docs.c.order_id == Order.id)
        .outerjoin(paid, paid.c.order_id == Order.id)
        .where(func.coalesce(status.c.status, "").notin_(tuple(EXCLUDED_FROM_AGGREGATES)))
        .subquery()
    )
    owed_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(owed.c.owed), 0),
                func.count(),
            )
            .select_from(owed)
            .where(owed.c.owed > 0)
        )
    ).one()

    # Status board.
    status2 = latest_status_subquery()
    # Built once and used in both the SELECT and the GROUP BY. Calling
    # coalesce() twice produces two separate bind parameters ($1 and $2), and
    # PostgreSQL matches grouping expressions structurally — two Param nodes
    # with different ids are not equal, so it falls back to requiring the bare
    # column and rejects the query with "must appear in the GROUP BY clause".
    # The SQL text looks identical either way; only the parameter ids differ,
    # which is why this compiles cleanly and fails only against a database.
    current_status = func.coalesce(status2.c.status, "new")
    status_rows = (
        await db.execute(
            select(
                current_status.label("status"),
                func.count().label("count"),
            )
            .select_from(Order)
            .outerjoin(status2, status2.c.order_id == Order.id)
            .group_by(current_status)
        )
    ).all()

    status3 = latest_status_subquery()
    open_and_overdue = (
        await db.execute(
            select(
                func.count().label("open"),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    Order.due_date.is_not(None),
                                    Order.due_date < today,
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label("overdue"),
            )
            .select_from(Order)
            .outerjoin(status3, status3.c.order_id == Order.id)
            .where(func.coalesce(status3.c.status, "new").notin_(tuple(CLOSED_STATUSES)))
        )
    ).one()

    return DashboardSummary(
        all_time=all_time,
        awaiting_payment=Decimal(owed_row[0] or 0),
        awaiting_payment_orders=int(owed_row[1] or 0),
        this_month=Comparison(
            days_elapsed=days_elapsed,
            current=current,
            previous_partial=previous_partial,
            previous_full=previous_full,
            revenue_change=_change(current.revenue, previous_partial.revenue),
            profit_change=_change(current.profit, previous_partial.profit),
            orders_change=_change(current.orders, previous_partial.orders),
        ),
        by_status=[StatusCount(status=r[0], count=int(r[1])) for r in status_rows],
        open_orders=int(open_and_overdue[0] or 0),
        overdue_orders=int(open_and_overdue[1] or 0),
    )


@router.get("/summary", response_model=ReportSummary)
async def summary(
    db: Db,
    _: Annotated[object, Depends(require(Permission.REPORTS_PROFIT))],
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    months: Annotated[int, Query(ge=1, le=36)] = 12,
) -> ReportSummary:
    period = await _totals(db, start, end)

    # Monthly trends — grouped in SQL, not by looping months in Python.
    status = latest_status_subquery()
    docs = document_totals_subquery()
    expenses = order_expenses_subquery()

    per_order = (
        select(
            func.to_char(Order.order_date, "YYYY-MM").label("month"),
            (func.coalesce(docs.c.documents_total, 0) + Order.delivery_cost).label("revenue"),
            (
                func.coalesce(docs.c.gross_profit, 0)
                + Order.delivery_cost
                - func.coalesce(expenses.c.expenses, 0)
            ).label("profit"),
        )
        .outerjoin(status, status.c.order_id == Order.id)
        .outerjoin(docs, docs.c.order_id == Order.id)
        .outerjoin(expenses, expenses.c.order_id == Order.id)
        .where(func.coalesce(status.c.status, "").notin_(tuple(EXCLUDED_FROM_AGGREGATES)))
        .subquery()
    )

    trend_rows = (
        await db.execute(
            select(
                per_order.c.month,
                func.coalesce(func.sum(per_order.c.revenue), 0),
                func.coalesce(func.sum(per_order.c.profit), 0),
                func.count(),
            )
            .group_by(per_order.c.month)
            .order_by(per_order.c.month.desc())
            .limit(months)
        )
    ).all()

    # Cost breakdown.
    status_b = latest_status_subquery()
    docs_b = document_totals_subquery()
    expenses_b = order_expenses_subquery()
    cost_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(docs_b.c.translator_total), 0),
                func.coalesce(func.sum(docs_b.c.notary_total), 0),
                func.coalesce(func.sum(Order.delivery_cost), 0),
                func.coalesce(func.sum(expenses_b.c.expenses), 0),
            )
            .select_from(Order)
            .outerjoin(status_b, status_b.c.order_id == Order.id)
            .outerjoin(docs_b, docs_b.c.order_id == Order.id)
            .outerjoin(expenses_b, expenses_b.c.order_id == Order.id)
            .where(func.coalesce(status_b.c.status, "").notin_(tuple(EXCLUDED_FROM_AGGREGATES)))
        )
    ).one()

    # By client type.
    status_c = latest_status_subquery()
    docs_c = document_totals_subquery()
    expenses_c = order_expenses_subquery()
    type_rows = (
        await db.execute(
            select(
                Client.client_type,
                func.coalesce(
                    func.sum(func.coalesce(docs_c.c.documents_total, 0) + Order.delivery_cost), 0
                ),
                func.coalesce(
                    func.sum(
                        func.coalesce(docs_c.c.gross_profit, 0)
                        + Order.delivery_cost
                        - func.coalesce(expenses_c.c.expenses, 0)
                    ),
                    0,
                ),
                func.count(),
            )
            .select_from(Order)
            .join(Client, Client.id == Order.client_id)
            .outerjoin(status_c, status_c.c.order_id == Order.id)
            .outerjoin(docs_c, docs_c.c.order_id == Order.id)
            .outerjoin(expenses_c, expenses_c.c.order_id == Order.id)
            .where(func.coalesce(status_c.c.status, "").notin_(tuple(EXCLUDED_FROM_AGGREGATES)))
            .group_by(Client.client_type)
        )
    ).all()

    return ReportSummary(
        period=period,
        # Reversed so the chart reads oldest to newest left to right.
        monthly_trends=[
            MonthlyPoint(
                month=r[0], revenue=Decimal(r[1] or 0), profit=Decimal(r[2] or 0), orders=int(r[3])
            )
            for r in reversed(trend_rows)
        ],
        cost_breakdown=CostBreakdown(
            translator=Decimal(cost_row[0] or 0),
            notary=Decimal(cost_row[1] or 0),
            delivery=Decimal(cost_row[2] or 0),
            expenses=Decimal(cost_row[3] or 0),
        ),
        by_client_type=[
            ByClientType(
                client_type=r[0],
                revenue=Decimal(r[1] or 0),
                profit=Decimal(r[2] or 0),
                orders=int(r[3]),
            )
            for r in type_rows
        ],
    )
