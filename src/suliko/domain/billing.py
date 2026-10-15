"""What a bureau owes for a month: its package, the orders it made, and the extras.

A bureau is on one of three packages (`models.billing.BillingPackage`). A package
covers some number of orders a month for a monthly fee; each order beyond them
costs a fixed amount, on every package. Nothing is ever blocked: past the
included orders the bureau is told, and the extras are added to what it is asked
to pay (decided 2026-10-15).

## What counts as an order

An order made (``orders.created_at``) in the calendar month, in the BUREAU's own
time zone, whose current status is not cancelled or rejected. Orders brought in by
a spreadsheet import (``source = 'import'``) are not counted, and neither is
anything a freelancer or a personal account does. The import exception is a
known loophole, left on purpose: a bureau cannot dodge the count by importing,
because the count is of what was MADE here, but history is not billed.

The current status is read with a correlated subquery rather than the
``DISTINCT ON`` the reports use, so the same statement runs on the SQLite the
tests use. Counting one bureau's month is a few hundred rows.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from suliko.core.errors import ValidationError
from suliko.domain.clock import today_in, zone
from suliko.domain.plans import TenantPlan, effective_plan
from suliko.domain.statuses import EXCLUDED_FROM_AGGREGATES
from suliko.models.billing import BillingPackage
from suliko.models.order import Order, OrderStatusEvent
from suliko.models.tenant import Tenant

TIERS: tuple[int, ...] = (1, 2, 3)

#: From this share of the included orders the bureau is warned (80 percent).
WARN_NUMERATOR, WARN_DENOMINATOR = 4, 5

#: What the packages are until a superuser changes them, and what the migration seeds.
DEFAULT_PACKAGES: dict[int, tuple[int, int | None, int]] = {
    1: (50, 5000, 100),
    2: (150, 10000, 100),
    3: (500, None, 100),
}


@dataclass(frozen=True)
class Package:
    tier: int
    orders_included: int
    #: Tetri. None: agreed with each bureau, so there is no price to show.
    monthly_price_cents: int | None
    extra_order_cents: int


def default_package(tier: int) -> Package:
    included, price, extra = DEFAULT_PACKAGES[tier]
    return Package(tier, included, price, extra)


def package_of(row: BillingPackage) -> Package:
    return Package(row.tier, row.orders_included, row.monthly_price_cents, row.extra_order_cents)


async def load_packages(db: Any) -> dict[int, Package]:
    """The three packages as stored. One that is missing falls back to its default,
    so a bureau is never without a package because a row was deleted."""
    rows = (await db.execute(select(BillingPackage))).scalars().all()
    stored = {row.tier: package_of(row) for row in rows}
    return {tier: stored.get(tier) or default_package(tier) for tier in TIERS}


class Level(enum.StrEnum):
    #: Under 80 percent of the included orders.
    OK = "ok"
    #: 80 percent or more, not yet all of them.
    NEAR = "near"
    #: All of them used. Orders past this are charged.
    LIMIT = "limit"


def level_of(orders: int, included: int) -> Level:
    if orders >= included:
        return Level.LIMIT
    if orders * WARN_DENOMINATOR >= included * WARN_NUMERATOR:
        return Level.NEAR
    return Level.OK


@dataclass(frozen=True)
class Quote:
    """A month's figures for one bureau on one package."""

    orders: int
    included: int
    extra_orders: int
    level: Level
    plan_fee_cents: int | None
    extra_fee_cents: int
    #: The monthly fee (when it has a price) plus the extras.
    total_cents: int


def quote(package: Package, orders: int) -> Quote:
    extra = max(0, orders - package.orders_included)
    extra_fee = extra * package.extra_order_cents
    return Quote(
        orders=orders,
        included=package.orders_included,
        extra_orders=extra,
        level=level_of(orders, package.orders_included),
        plan_fee_cents=package.monthly_price_cents,
        extra_fee_cents=extra_fee,
        total_cents=(package.monthly_price_cents or 0) + extra_fee,
    )


# ── Money ───────────────────────────────────────────────────────────────────


def to_cents(amount: Decimal) -> int:
    """Lari to whole tetri, rounded half up."""
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


# ── Months ──────────────────────────────────────────────────────────────────

_PERIOD = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def period_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def parse_period(text: str) -> tuple[int, int]:
    match = _PERIOD.match(text or "")
    if not match:
        raise ValidationError("Choose a month as YYYY-MM, for example 2026-10.")
    return int(match.group(1)), int(match.group(2))


def current_period(tz: str | None, *, now: datetime | None = None) -> str:
    """The calendar month it is in the bureau's time zone."""
    return period_of(today_in(tz, now=now))


def month_window(period: str, tz: str | None) -> tuple[datetime, datetime]:
    """The month from its first local midnight to the next, as UTC moments.

    Half open: the first instant of the next month belongs to the next month.
    """
    year, month = parse_period(period)
    local: ZoneInfo = zone(tz)
    start = datetime(year, month, 1, tzinfo=local)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=local)
    return start.astimezone(UTC), end.astimezone(UTC)


# ── Who is counted, and what is counted ─────────────────────────────────────


def is_counted(tenant: Tenant) -> bool:
    """A bureau, not a freelancer and not a person's own workspace.

    A tenant that has not finished onboarding is treated as a freelancer
    (`effective_plan`), so it is not counted either.
    """
    return effective_plan(tenant.plan) is TenantPlan.BUREAU and not tenant.is_personal


async def count_orders(db: Any, tenant_id: int, period: str, tz: str | None) -> int:
    """The orders that count for a bureau in a month. See the module docstring.

    Filters on an explicit ``tenant_id``, so it works from the bureau's own
    session and from the platform area, which reads across tenants.
    """
    start, end = month_window(period, tz)
    latest = (
        select(OrderStatusEvent.status)
        .where(OrderStatusEvent.order_id == Order.id)
        .order_by(OrderStatusEvent.changed_at.desc(), OrderStatusEvent.id.desc())
        .limit(1)
        .correlate(Order)
        .scalar_subquery()
    )
    count = await db.scalar(
        select(func.count())
        .select_from(Order)
        .where(
            Order.tenant_id == tenant_id,
            Order.created_at >= start,
            Order.created_at < end,
            or_(Order.source.is_(None), Order.source != "import"),
            func.coalesce(latest, "new").notin_(sorted(EXCLUDED_FROM_AGGREGATES)),
        )
    )
    return int(count or 0)
