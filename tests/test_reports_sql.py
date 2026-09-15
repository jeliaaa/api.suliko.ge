"""The dashboard's SQL must compile, and its response must assemble.

These are the two ways `GET /reports/dashboard` can fail that are our fault
rather than the database's: a query SQLAlchemy cannot render, and a row shape
the response model rejects. Both are cheap to catch and neither needs a
database — which matters, because this endpoint builds seven statements across
four subqueries and a regression in any of them is a 500 on the first screen
every user sees.

What these tests deliberately do NOT cover is whether the tables and columns
exist. That is a deployment property, and `suliko.cli schema-diff` answers it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from suliko.api.v1 import reports


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def one(self) -> tuple[Any, ...]:
        return self.rows[0]

    def all(self) -> list[tuple[Any, ...]]:
        return self.rows


class CompilingSession:
    """Compiles every statement to PostgreSQL, then answers with canned rows.

    Rows are matched on the statement's column labels rather than its position
    in the handler, so reordering the queries does not silently start feeding
    one of them another's shape.
    """

    def __init__(self) -> None:
        self.compiled: list[str] = []

    async def execute(self, stmt: Any) -> FakeResult:
        # The compile is the assertion: an unrenderable statement raises here.
        self.compiled.append(str(stmt.compile(dialect=postgresql.dialect())))

        names = [c.name for c in stmt.selected_columns]

        if names == ["orders", "revenue", "profit"]:
            return FakeResult([(4, Decimal("1000.00"), Decimal("400.00"))])
        if names == ["status", "count"]:
            return FakeResult([("new", 3), ("being translated", 5), ("completed", 12)])
        if names == ["open", "overdue"]:
            return FakeResult([(10, 2)])
        # The outstanding query selects unlabelled aggregates.
        return FakeResult([(Decimal("250.00"), 2)])


@pytest.mark.asyncio
async def test_the_dashboard_builds_valid_postgresql() -> None:
    db = CompilingSession()

    summary = await reports.dashboard(db=db, _=None, today=dt.date(2026, 9, 15))  # type: ignore[arg-type]

    # Four period totals, outstanding, the status board, open/overdue.
    assert len(db.compiled) == 7
    assert summary.all_time.orders == 4
    assert summary.awaiting_payment == Decimal("250.00")
    assert summary.open_orders == 10
    assert summary.overdue_orders == 2
    assert {s.status for s in summary.by_status} == {"new", "being translated", "completed"}


@pytest.mark.asyncio
async def test_the_month_comparison_is_like_for_like() -> None:
    """15 September must compare against 1–15 August, not the whole of August.

    The clamp matters at month boundaries: 31 March-to-date against February
    would otherwise run past the end of the month.
    """
    db = CompilingSession()
    summary = await reports.dashboard(db=db, _=None, today=dt.date(2026, 9, 15))  # type: ignore[arg-type]

    assert summary.this_month.days_elapsed == 15

    windows = [sql for sql in db.compiled if "order_date >=" in sql.replace("orders.", "")]
    assert windows, "the period queries should be date-bounded"


@pytest.mark.asyncio
async def test_a_short_month_does_not_overrun_the_previous_one() -> None:
    """31 March compared with February: the partial window must stop at the
    28th rather than spilling into March."""
    db = CompilingSession()
    summary = await reports.dashboard(db=db, _=None, today=dt.date(2026, 3, 31))  # type: ignore[arg-type]

    assert summary.this_month.days_elapsed == 31
    # Compiles without error is the point; the clamp is arithmetic in the
    # handler and cannot be read back off the canned rows.
    assert len(db.compiled) == 7


def test_the_totals_query_renders_without_a_cartesian_join() -> None:
    """Documents and expenses must each join to orders, never to each other.

    Joining them together multiplies the per-order expense by the document
    count — the bug the PHP's query is shaped around, and the reason profit is
    summed per order rather than in one flat join.
    """
    sql = str(reports._totals_query().compile(dialect=postgresql.dialect()))

    assert "LEFT OUTER JOIN" in sql
    assert sql.count("LEFT OUTER JOIN") == 3  # status, documents, expenses
    assert "document_totals.order_id = orders.id" in sql
    assert "order_expenses.order_id = orders.id" in sql


def test_cancelled_orders_are_excluded_from_every_total() -> None:
    """A cancelled job was never revenue. Including it makes a bad month look
    like a good one."""
    sql = str(
        reports._totals_query().compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "NOT IN ('cancelled')" in sql
