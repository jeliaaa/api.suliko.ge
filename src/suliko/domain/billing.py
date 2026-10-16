"""What an account may order in a month: its package, and what it costs.

Every account (a bureau, a freelancer, a person's own workspace) is on one of three
packages (`models.billing.BillingPackage`). A package covers some number of orders
a month for a monthly fee. When the month's orders reach the package, NEW orders
are refused until the account moves up a package (`api/v1/billing.py`, one click
and a short confirmation); nothing is charged per order (decided 2026-10-17, which
replaced "warn, never block, 1 GEL per extra order").

Bureaus and freelancers have their own set of three packages, so the numbers and
prices can differ between them.

## What counts as an order

An order made (``orders.created_at``) in the calendar month, in the account's own
time zone, whose current status is not cancelled or rejected. Orders brought in by
a spreadsheet import (``source = 'import'``) are not counted: history is not
billed and cannot be blocked.

The current status is read with a correlated subquery rather than the
``DISTINCT ON`` the reports use, so the same statement runs on the SQLite the
tests use. Counting one account's month is a few hundred rows.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import status as http_status
from sqlalchemy import func, or_, select

from suliko.core.errors import AppError, ValidationError
from suliko.domain.clock import today_in, zone
from suliko.domain.plans import TenantPlan, effective_plan
from suliko.domain.statuses import EXCLUDED_FROM_AGGREGATES
from suliko.models.billing import BillingFreeMonth, BillingPackage
from suliko.models.order import Order, OrderStatusEvent
from suliko.models.tenant import Tenant

TIERS: tuple[int, ...] = (1, 2, 3)

Audience = Literal["bureau", "freelancer"]
AUDIENCES: tuple[Audience, ...] = ("bureau", "freelancer")

#: From this share of the included orders the account is warned (80 percent).
WARN_NUMERATOR, WARN_DENOMINATOR = 4, 5

#: The terms an upgrade is confirmed against. Change it when the wording changes:
#: the screen sends back the version it showed, and an old one is refused.
TERMS_VERSION = "2026-10-17"

#: What the packages are until a superuser changes them, and what the migration
#: seeds: audience -> tier -> (orders included, monthly fee in tetri or None).
#: PLACEHOLDERS: the freelancer numbers in particular are not decided.
DEFAULT_PACKAGES: dict[str, dict[int, tuple[int, int | None]]] = {
    "bureau": {1: (50, 5000), 2: (150, 10000), 3: (500, None)},
    "freelancer": {1: (50, 0), 2: (150, 2000), 3: (500, None)},
}


@dataclass(frozen=True)
class Package:
    audience: str
    tier: int
    orders_included: int
    #: Tetri. 0 is free. None: agreed with each account, so there is no price to show
    #: and the account cannot move to it by itself.
    monthly_price_cents: int | None


def default_package(audience: str, tier: int) -> Package:
    included, price = DEFAULT_PACKAGES[audience][tier]
    return Package(audience, tier, included, price)


def package_of(row: BillingPackage) -> Package:
    return Package(row.audience, row.tier, row.orders_included, row.monthly_price_cents)


async def load_packages(db: Any, audience: str) -> dict[int, Package]:
    """The three packages of one audience as stored. A missing one falls back to its
    default, so an account is never without a package because a row was deleted."""
    rows = (
        (await db.execute(select(BillingPackage).where(BillingPackage.audience == audience)))
        .scalars()
        .all()
    )
    stored = {row.tier: package_of(row) for row in rows}
    return {tier: stored.get(tier) or default_package(audience, tier) for tier in TIERS}


def audience_of(tenant: Tenant) -> Audience:
    """Freelancers and a person's own workspace have their own packages."""
    if tenant.is_personal or effective_plan(tenant.plan) is TenantPlan.FREELANCER:
        return "freelancer"
    return "bureau"


class Level(enum.StrEnum):
    #: Under 80 percent of the included orders.
    OK = "ok"
    #: 80 percent or more, not yet all of them.
    NEAR = "near"
    #: All of them used. New orders are refused until the package is raised.
    LIMIT = "limit"


def level_of(orders: int, included: int) -> Level:
    if orders >= included:
        return Level.LIMIT
    if orders * WARN_DENOMINATOR >= included * WARN_NUMERATOR:
        return Level.NEAR
    return Level.OK


@dataclass(frozen=True)
class Quote:
    """A month's figures for one account on one package."""

    orders: int
    included: int
    remaining: int
    level: Level
    #: The monthly fee, or None when there is no price.
    plan_fee_cents: int | None

    @property
    def blocked(self) -> bool:
        return self.level is Level.LIMIT

    @property
    def total_cents(self) -> int:
        return self.plan_fee_cents or 0


def quote(package: Package, orders: int) -> Quote:
    return Quote(
        orders=orders,
        included=package.orders_included,
        remaining=max(0, package.orders_included - orders),
        level=level_of(orders, package.orders_included),
        plan_fee_cents=package.monthly_price_cents,
    )


class PackageLimitError(AppError):
    """The month's orders have reached the package: raise it to add more."""

    status_code = http_status.HTTP_402_PAYMENT_REQUIRED
    error_code = "package_limit_reached"


# ── Money ───────────────────────────────────────────────────────────────────


def to_cents(amount: Decimal) -> int:
    """Lari to whole tetri, rounded half up."""
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


# ── Months ──────────────────────────────────────────────────────────────────

_PERIOD = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def parse_period(text: str) -> tuple[int, int]:
    match = _PERIOD.match(text or "")
    if not match:
        raise ValidationError("Choose a month as YYYY-MM, for example 2026-10.")
    return int(match.group(1)), int(match.group(2))


def current_period(tz: str | None, *, now: datetime | None = None) -> str:
    """The calendar month it is in the account's time zone."""
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


def period_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def period_days(period: str) -> tuple[date, date]:
    """The first and the last day of a calendar month."""
    year, month = parse_period(period)
    first = date(year, month, 1)
    return first, date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)


def previous_period(period: str) -> str:
    return period_of(period_days(period)[0] - timedelta(days=1))


def started_on(tenant: Tenant) -> date:
    """The day the account began, in its own time zone: where its billing starts."""
    return tenant.created_at.astimezone(zone(tenant.timezone)).date()


async def free_periods(db: Any, tenant_id: int) -> set[str]:
    """The months a superuser made free for this account."""
    rows = await db.execute(
        select(BillingFreeMonth.period).where(BillingFreeMonth.tenant_id == tenant_id)
    )
    return set(rows.scalars().all())


# ── What is counted ─────────────────────────────────────────────────────────


async def count_orders(db: Any, tenant_id: int, period: str, tz: str | None) -> int:
    """The orders that count for an account in a month. See the module docstring.

    Filters on an explicit ``tenant_id``, so it works from the account's own
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


async def month_of(db: Any, tenant: Tenant, period: str | None = None) -> tuple[Package, Quote]:
    """An account's package and its figures for a month (this one by default)."""
    packages = await load_packages(db, audience_of(tenant))
    package = packages.get(tenant.package_tier) or packages[1]
    chosen = period or current_period(tenant.timezone)
    return package, quote(package, await count_orders(db, tenant.id, chosen, tenant.timezone))


async def ensure_room_for_order(db: Any, tenant: Tenant) -> None:
    """Refuse a NEW order once this month's orders have reached the package."""
    package, figures = await month_of(db, tenant)
    if figures.blocked:
        raise PackageLimitError(
            f"All {figures.included} orders of this month's package are used. "
            "Move to a bigger package to add more orders.",
            included=figures.included,
            tier=package.tier,
        )
