"""Dashboard and report aggregates.

One router, because the dashboard cards and the reports charts are the same
figures over different windows.

## The profit shape

Profit is computed per order and then summed, never as one flat join. Joining
documents and expenses together multiplies the expense by the document count —
the bug the PHP's query is deliberately shaped around.

Profit is price minus translator, notary and order expenses. The courier fee
is revenue (the client pays it) but NOT profit and NOT a cost: it is passed
through to the courier (decided 2026-09-24, as the PHP).

## Excluded orders

Cancelled and rejected orders are excluded from every figure here. Neither was
ever revenue, and including them makes a bad month look like a good one.

## One window for everything

Every figure on the Reports screen is over the SAME period. The first cut
applied the chosen dates to the headline totals only, and showed all-time cost
breakdown and client-type figures underneath them, labelled with the period.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import Db, require
from suliko.domain.clock import today_in
from suliko.domain.orders import (
    document_totals_subquery,
    latest_status_subquery,
    order_expenses_subquery,
    paid_subquery,
)
from suliko.domain.statuses import CLOSED_STATUSES, EXCLUDED_FROM_AGGREGATES, sql_values
from suliko.models.directory import Client, ClientType
from suliko.models.order import Order
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

router = APIRouter(prefix="/reports", tags=["reports"])


class PeriodTotals(BaseModel):
    orders: int
    revenue: Decimal
    #: Null without `reports.profit`.
    profit: Decimal | None
    #: profit / revenue, or null when there is no revenue to divide by (or
    #: the caller may not see profit).
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
    #: The date the figures were computed for, in the bureau's timezone.
    today: date


class MonthlyPoint(BaseModel):
    month: str
    revenue: Decimal
    profit: Decimal
    orders: int


class CostBreakdown(BaseModel):
    """What the bureau paid out to do the work. The courier fee is not here:
    it is passed through, not a cost — see `ReportSummary.delivery_collected`."""

    translator: Decimal
    notary: Decimal
    expenses: Decimal


class ByClientType(BaseModel):
    client_type: ClientType
    revenue: Decimal
    profit: Decimal
    orders: int


class ReportSummary(BaseModel):
    start: date | None
    end: date | None
    period: PeriodTotals
    monthly_trends: list[MonthlyPoint]
    cost_breakdown: CostBreakdown
    #: Courier fees in the period's revenue, passed through to the courier.
    delivery_collected: Decimal
    by_client_type: list[ByClientType]


def _per_order(start: date | None = None, end: date | None = None) -> Select[Any]:
    """One row per counted order in the window, with every figure reports use.

    Revenue includes the courier fee (the client pays it); profit does not.
    Built per order and aggregated by the callers, so the expense subtraction
    happens once per order rather than once per document.
    """
    status = latest_status_subquery()
    docs = document_totals_subquery()
    expenses = order_expenses_subquery()

    stmt = (
        select(
            Order.id.label("order_id"),
            Order.order_date.label("order_date"),
            Order.client_id.label("client_id"),
            (func.coalesce(docs.c.documents_total, 0) + Order.delivery_cost).label("revenue"),
            (
                func.coalesce(docs.c.gross_profit, 0) - func.coalesce(expenses.c.expenses, 0)
            ).label("profit"),
            func.coalesce(docs.c.translator_total, 0).label("translator"),
            func.coalesce(docs.c.notary_total, 0).label("notary"),
            Order.delivery_cost.label("delivery"),
            func.coalesce(expenses.c.expenses, 0).label("expenses"),
        )
        .outerjoin(status, status.c.order_id == Order.id)
        .outerjoin(docs, docs.c.order_id == Order.id)
        .outerjoin(expenses, expenses.c.order_id == Order.id)
        .where(func.coalesce(status.c.status, "").notin_(sql_values(EXCLUDED_FROM_AGGREGATES)))
    )
    if start is not None:
        stmt = stmt.where(Order.order_date >= start)
    if end is not None:
        stmt = stmt.where(Order.order_date <= end)
    return stmt


def _totals_query(start: date | None = None, end: date | None = None) -> Select[Any]:
    """Revenue, profit and order count over a date window."""
    sub = _per_order(start, end).subquery()
    return select(
        func.count().label("orders"),
        func.coalesce(func.sum(sub.c.revenue), 0).label("revenue"),
        func.coalesce(func.sum(sub.c.profit), 0).label("profit"),
    ).select_from(sub)


async def _totals(
    db: AsyncSession,
    start: date | None = None,
    end: date | None = None,
    *,
    show_profit: bool = True,
) -> PeriodTotals:
    row = (await db.execute(_totals_query(start, end))).one()
    orders, revenue, profit = int(row[0] or 0), Decimal(row[1] or 0), Decimal(row[2] or 0)
    return PeriodTotals(
        orders=orders,
        revenue=revenue,
        profit=profit if show_profit else None,
        margin=float(profit / revenue) if revenue and show_profit else None,
    )


def _change(current: Decimal | int | None, previous: Decimal | int | None) -> float | None:
    """Ratio of change, or None when there is no baseline.

    Returning None rather than 0 or infinity: "up 100% from nothing" is
    meaningless, and the UI should omit the figure instead of printing one.
    """
    if not previous or current is None:
        return None
    return float((Decimal(current) - Decimal(previous)) / Decimal(previous))


@router.get("/dashboard", response_model=DashboardSummary)
async def dashboard(
    db: Db,
    session: Annotated[AuthenticatedSession, Depends(require(Permission.REPORTS_READ))],
    today: date | None = None,
) -> DashboardSummary:
    # `today` is injectable so the month-boundary arithmetic is testable
    # without freezing the clock. Otherwise it is the BUREAU's today: in UTC,
    # the first four hours of a Tbilisi month still belong to the last one.
    today = today or today_in(session.timezone)
    show_profit = session.has(Permission.REPORTS_PROFIT)

    month_start = today.replace(day=1)
    days_elapsed = (today - month_start).days + 1

    previous_month_end = month_start - timedelta(days=1)
    previous_month_start = previous_month_end.replace(day=1)
    # Clamped: comparing 31 March-to-date against February would run past the
    # end of the month and silently include March days.
    previous_partial_end = min(
        previous_month_start + timedelta(days=days_elapsed - 1), previous_month_end
    )

    all_time = await _totals(db, show_profit=show_profit)
    current = await _totals(db, month_start, today, show_profit=show_profit)
    previous_partial = await _totals(
        db, previous_month_start, previous_partial_end, show_profit=show_profit
    )
    previous_full = await _totals(
        db, previous_month_start, previous_month_end, show_profit=show_profit
    )

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
        .where(func.coalesce(status.c.status, "").notin_(sql_values(EXCLUDED_FROM_AGGREGATES)))
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
            .where(func.coalesce(status3.c.status, "new").notin_(sql_values(CLOSED_STATUSES)))
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
        today=today,
    )


@router.get("/summary", response_model=ReportSummary)
async def summary(
    db: Db,
    _: Annotated[object, Depends(require(Permission.REPORTS_PROFIT))],
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    months: Annotated[int, Query(ge=1, le=36)] = 12,
) -> ReportSummary:
    """Every figure over one window: `start`..`end` when given, else all time.

    The monthly trend is also restricted to the window; with no window it is
    the most recent `months` months.
    """
    period = await _totals(db, start, end)
    window = _per_order(start, end).subquery("window")

    # Monthly trends — grouped in SQL, not by looping months in Python. The
    # to_char is built once for SELECT and GROUP BY; see the note on
    # `current_status` in `dashboard` for why that matters.
    month = func.to_char(window.c.order_date, "YYYY-MM")
    trend_rows = (
        await db.execute(
            select(
                month.label("month"),
                func.coalesce(func.sum(window.c.revenue), 0),
                func.coalesce(func.sum(window.c.profit), 0),
                func.count(),
            )
            .group_by(month)
            .order_by(month.desc())
            .limit(months)
        )
    ).all()

    cost_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(window.c.translator), 0),
                func.coalesce(func.sum(window.c.notary), 0),
                func.coalesce(func.sum(window.c.expenses), 0),
                func.coalesce(func.sum(window.c.delivery), 0),
            )
        )
    ).one()

    type_rows = (
        await db.execute(
            select(
                Client.client_type,
                func.coalesce(func.sum(window.c.revenue), 0),
                func.coalesce(func.sum(window.c.profit), 0),
                func.count(),
            )
            .select_from(window)
            .join(Client, Client.id == window.c.client_id)
            .group_by(Client.client_type)
        )
    ).all()

    return ReportSummary(
        start=start,
        end=end,
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
            expenses=Decimal(cost_row[2] or 0),
        ),
        delivery_collected=Decimal(cost_row[3] or 0),
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
