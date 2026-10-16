"""Order packages: counting a month, the block at the limit, the move up, the invoices.

The arithmetic (`domain/billing.py`) is pure and tested directly. The counting
and the endpoints run for real against in-memory SQLite, with the tenant filter
installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from suliko.api.v1 import billing as tenant_api
from suliko.api.v1 import orders as orders_api
from suliko.api.v1 import platform_billing as platform_api
from suliko.api.v1.router import api_router
from suliko.core.errors import ConflictError, NotFoundError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain import billing
from suliko.domain.plans import TenantPlan
from suliko.models.billing import (
    BillingFreeMonth,
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPackage,
)
from suliko.models.order import Order, OrderStatusEvent
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role
from suliko.security.permissions import permissions_for_role
from suliko.security.sessions import AuthenticatedSession

install_tenant_filter()

ACME, GLOBEX, SOLO, PERSONAL = 1, 2, 3, 4


# ── The arithmetic ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("orders", "level"),
    [(0, "ok"), (39, "ok"), (40, "near"), (49, "near"), (50, "limit"), (51, "limit")],
)
def test_the_warning_starts_at_eighty_percent_and_the_limit_at_all_of_them(
    orders: int, level: str
) -> None:
    assert billing.level_of(orders, 50) == level


def test_a_small_package_warns_at_the_right_order() -> None:
    # 80 percent of 10 is 8; 80 percent of 7 is 5.6, so six orders is the first past it.
    assert billing.level_of(7, 10) == "ok"
    assert billing.level_of(8, 10) == "near"
    assert billing.level_of(5, 7) == "ok"
    assert billing.level_of(6, 7) == "near"


def test_at_the_limit_the_account_is_blocked_and_nothing_is_charged_per_order() -> None:
    package = billing.default_package("bureau", 1)
    below = billing.quote(package, package.orders_included - 1)
    assert (below.blocked, below.remaining, below.total_cents) == (False, 1, 5000)
    at = billing.quote(package, package.orders_included)
    assert (at.blocked, at.remaining) == (True, 0)
    # Past it (an order made before the rule, say) the fee is still just the fee.
    over = billing.quote(package, package.orders_included + 30)
    assert (over.blocked, over.remaining, over.total_cents) == (True, 0, 5000)


def test_a_package_can_be_free_or_have_no_price() -> None:
    free = billing.default_package("freelancer", 1)
    assert (free.monthly_price_cents, billing.quote(free, 3).total_cents) == (0, 0)
    quoted = billing.default_package("bureau", 3)
    assert quoted.monthly_price_cents is None
    assert billing.quote(quoted, 10).total_cents == 0


def test_bureaus_and_freelancers_have_their_own_packages() -> None:
    assert {a: set(p) for a, p in billing.DEFAULT_PACKAGES.items()} == {
        "bureau": {1, 2, 3},
        "freelancer": {1, 2, 3},
    }
    assert billing.default_package("bureau", 2) != billing.default_package("freelancer", 2)


def test_lari_and_tetri_round_trip_and_round_half_up() -> None:
    assert billing.to_cents(Decimal("50")) == 5000
    assert billing.to_cents(Decimal("0.005")) == 1
    assert billing.to_cents(Decimal("12.345")) == 1235
    assert billing.from_cents(5000) == Decimal("50.00")
    assert str(billing.from_cents(5)) == "0.05"


def test_a_month_is_the_accounts_own_calendar_month() -> None:
    start, end = billing.month_window("2026-10", "Asia/Tbilisi")
    # Tbilisi is UTC+4: October starts at 20:00 UTC on the 30th of September.
    assert start == datetime(2026, 9, 30, 20, tzinfo=UTC)
    assert end == datetime(2026, 10, 31, 20, tzinfo=UTC)
    assert billing.month_window("2026-12", "Asia/Tbilisi")[1] == datetime(
        2026, 12, 31, 20, tzinfo=UTC
    )
    night = datetime(2026, 10, 31, 20, 30, tzinfo=UTC)
    assert billing.current_period("Asia/Tbilisi", now=night) == "2026-11"
    assert billing.current_period("UTC", now=night) == "2026-10"


@pytest.mark.parametrize("bad", ["", "2026", "2026-13", "2026-1", "26-10", "october"])
def test_a_month_must_be_written_year_dash_month(bad: str) -> None:
    with pytest.raises(ValidationError):
        billing.parse_period(bad)


# ── Fixtures ────────────────────────────────────────────────────────────────

MODELS = (Tenant, Order, OrderStatusEvent, BillingPackage, BillingInvoice, BillingFreeMonth)


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def record(_db: object, _session: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("suliko.core.audit.record", record)
    return calls


def _tenant(tenant_id: int, plan: str | None, *, personal: bool = False, tier: int = 1) -> Tenant:
    return Tenant(
        id=tenant_id,
        slug=f"t{tenant_id}",
        display_name=f"Tenant {tenant_id}",
        status=TenantStatus.ACTIVE,
        plan=plan,
        locale="ka",
        is_personal=personal,
        package_tier=tier,
    )


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[Base.metadata.tables[m.__tablename__] for m in MODELS]
            )
        )
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as session:
        with bypass_tenant_scope():
            session.add_all(
                [
                    _tenant(ACME, "bureau"),
                    _tenant(GLOBEX, "bureau", tier=2),
                    _tenant(SOLO, "freelancer"),
                    _tenant(PERSONAL, "freelancer", personal=True),
                ]
            )
            await session.commit()
        yield session
    await engine.dispose()


def when(day: int, hour: int = 12, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


async def make_order(
    db: AsyncSession,
    tenant_id: int,
    created: datetime,
    *,
    source: str | None = "office",
    statuses: tuple[str, ...] = (),
) -> Order:
    with bypass_tenant_scope():
        order = Order(
            tenant_id=tenant_id,
            client_id=1,
            order_date=created.date(),
            source=source,
            created_at=created,
            created_by_user_id=1,
        )
        db.add(order)
        await db.flush()
        for step, status in enumerate(statuses):
            db.add(
                OrderStatusEvent(
                    tenant_id=tenant_id,
                    order_id=order.id,
                    status=status,
                    changed_at=created.replace(minute=step),
                )
            )
        await db.flush()
    return order


async def fill(db: AsyncSession, tenant_id: int, count: int, *, month: int = 10) -> None:
    for number in range(count):
        await make_order(db, tenant_id, when(1 + number % 28, hour=number % 24, month=month))


async def counted(db: AsyncSession, tenant_id: int = ACME, period: str = "2026-10") -> int:
    with bypass_tenant_scope():
        return await billing.count_orders(db, tenant_id, period, "Asia/Tbilisi")


async def tenant_of(db: AsyncSession, tenant_id: int) -> Tenant:
    with bypass_tenant_scope():
        tenant = await db.get(Tenant, tenant_id)
    assert tenant is not None
    return tenant


def session_of(tenant_id: int, plan: TenantPlan = TenantPlan.BUREAU) -> AuthenticatedSession:
    return AuthenticatedSession(
        session_id=1,
        user_id=1,
        username="owner",
        full_name="Owner",
        email="o@x.test",
        role=Role.OWNER,
        tenant_id=tenant_id,
        tenant_slug=f"t{tenant_id}",
        tenant_name="Acme",
        plan=plan,
        onboarding_required=False,
        must_change_password=False,
        has_mfa=False,
        permissions=permissions_for_role(Role.OWNER),
        mfa_satisfied_at=None,
        impersonated_by_user_id=None,
    )


def superuser() -> AuthenticatedSession:
    return session_of(ACME)


# ── Counting ────────────────────────────────────────────────────────────────


async def test_only_orders_made_in_the_accounts_calendar_month_are_counted(
    db: AsyncSession,
) -> None:
    await make_order(db, ACME, when(15))
    # Half past eleven on 30 September in Tbilisi is still September...
    await make_order(db, ACME, datetime(2026, 9, 30, 19, 30, tzinfo=UTC))
    # ...and half past midnight on 1 October there is already October (UTC+4).
    await make_order(db, ACME, datetime(2026, 9, 30, 20, 30, tzinfo=UTC))
    # The same at the other end: 31 October 23:30 there is October, 1 November 00:30 is not.
    await make_order(db, ACME, datetime(2026, 10, 31, 19, 30, tzinfo=UTC))
    await make_order(db, ACME, datetime(2026, 10, 31, 20, 30, tzinfo=UTC))
    await make_order(db, ACME, when(20, month=9))
    assert await counted(db) == 3
    assert await counted(db, period="2026-11") == 1
    assert await counted(db, period="2026-09") == 2


async def test_cancelled_and_rejected_orders_are_not_counted_but_a_reopened_one_is(
    db: AsyncSession,
) -> None:
    await make_order(db, ACME, when(2))
    await make_order(db, ACME, when(3), statuses=("new", "confirmed"))
    await make_order(db, ACME, when(4), statuses=("new", "cancelled"))
    await make_order(db, ACME, when(5), statuses=("new", "rejected"))
    await make_order(db, ACME, when(6), statuses=("cancelled", "new"))
    assert await counted(db) == 3


async def test_imported_orders_are_not_counted(db: AsyncSession) -> None:
    await make_order(db, ACME, when(2), source="import")
    await make_order(db, ACME, when(3), source=None)
    await make_order(db, ACME, when(4), source="website")
    assert await counted(db) == 2


async def test_each_account_counts_only_its_own_orders(db: AsyncSession) -> None:
    await make_order(db, ACME, when(2))
    await make_order(db, GLOBEX, when(3))
    await make_order(db, GLOBEX, when(4))
    assert (await counted(db, ACME), await counted(db, GLOBEX)) == (1, 2)


async def test_a_bureau_a_freelancer_and_a_personal_workspace_each_have_their_audience(
    db: AsyncSession,
) -> None:
    assert billing.audience_of(await tenant_of(db, ACME)) == "bureau"
    assert billing.audience_of(await tenant_of(db, SOLO)) == "freelancer"
    assert billing.audience_of(await tenant_of(db, PERSONAL)) == "freelancer"
    # Not finished onboarding counts as a freelancer too, as for access.
    unfinished = await tenant_of(db, ACME)
    unfinished.plan = None
    assert billing.audience_of(unfinished) == "freelancer"


# ── The block ───────────────────────────────────────────────────────────────


async def test_an_order_is_allowed_up_to_the_last_one_and_refused_after(db: AsyncSession) -> None:
    await fill(db, ACME, 49)
    tenant = await tenant_of(db, ACME)
    with bypass_tenant_scope():
        await billing.ensure_room_for_order(db, tenant)
    await make_order(db, ACME, when(20, hour=3))
    with bypass_tenant_scope(), pytest.raises(billing.PackageLimitError) as refused:
        await billing.ensure_room_for_order(db, tenant)
    assert refused.value.status_code == 402
    assert refused.value.error_code == "package_limit_reached"
    assert refused.value.extra["included"] == 50


async def test_cancelling_an_order_makes_room_again(db: AsyncSession) -> None:
    await fill(db, ACME, 49)
    last = await make_order(db, ACME, when(20, hour=3), statuses=("new",))
    tenant = await tenant_of(db, ACME)
    with bypass_tenant_scope(), pytest.raises(billing.PackageLimitError):
        await billing.ensure_room_for_order(db, tenant)
    with bypass_tenant_scope():
        db.add(
            OrderStatusEvent(
                tenant_id=ACME, order_id=last.id, status="cancelled", changed_at=when(21)
            )
        )
        await db.flush()
        await billing.ensure_room_for_order(db, tenant)


async def test_a_freelancer_is_limited_by_the_freelancer_package(db: AsyncSession) -> None:
    await platform_api.edit_package(
        "freelancer",
        1,
        platform_api.PackageUpdate(orders_included=3, monthly_price=Decimal("0")),
        db,
        superuser(),
    )
    await fill(db, SOLO, 3)
    await fill(db, ACME, 3)
    with bypass_tenant_scope():
        with pytest.raises(billing.PackageLimitError):
            await billing.ensure_room_for_order(db, await tenant_of(db, SOLO))
        # A bureau with three orders is nowhere near its own 50.
        await billing.ensure_room_for_order(db, await tenant_of(db, ACME))


async def test_the_block_stands_at_the_first_line_of_create_order(db: AsyncSession) -> None:
    await fill(db, ACME, 50)
    payload = orders_api.OrderCreate.model_construct(client_id=1, documents=[])
    with tenant_scope(ACME), pytest.raises(billing.PackageLimitError):
        await orders_api.create_order(payload, db, session_of(ACME))


# ── The account's own view and the move up ──────────────────────────────────


async def test_usage_says_how_far_into_the_package_the_account_is(db: AsyncSession) -> None:
    await fill(db, ACME, 41)
    with tenant_scope(ACME):
        usage = await tenant_api.tenant_usage(db, await tenant_of(db, ACME), "2026-10")
    assert (usage.orders, usage.included, usage.remaining) == (41, 50, 9)
    assert (usage.level, usage.blocked, usage.tier) == ("near", False, 1)
    assert usage.monthly_price == Decimal("50.00")


async def test_a_freelancer_is_counted_against_its_own_package_too(db: AsyncSession) -> None:
    await fill(db, SOLO, 60)
    with tenant_scope(SOLO):
        usage = await tenant_api.tenant_usage(db, await tenant_of(db, SOLO), "2026-10")
    assert (usage.orders, usage.included, usage.blocked) == (60, 50, True)
    assert usage.monthly_price == Decimal("0.00")


async def test_the_packages_fall_back_to_the_defaults_when_none_are_stored(
    db: AsyncSession,
) -> None:
    for audience, expected in (
        ("bureau", {1: 50, 2: 150, 3: 500}),
        ("freelancer", {1: 50, 2: 150, 3: 500}),
    ):
        packages = await billing.load_packages(db, audience)
        assert {tier: p.orders_included for tier, p in packages.items()} == expected
    assert (await billing.load_packages(db, "bureau"))[3].monthly_price_cents is None


async def test_the_overview_lists_the_accounts_own_packages_the_way_up_and_asked_invoices(
    db: AsyncSession,
) -> None:
    with bypass_tenant_scope():
        for period, status in (
            ("2026-07", BillingInvoiceStatus.OPEN),
            ("2026-08", BillingInvoiceStatus.SENT),
            ("2026-09", BillingInvoiceStatus.PAID),
        ):
            db.add(
                BillingInvoice(
                    tenant_id=ACME,
                    period=period,
                    package_tier=1,
                    orders_included=50,
                    orders_counted=52,
                    plan_fee_cents=5000,
                    total_cents=5000,
                    status=status,
                )
            )
        await db.flush()
    with tenant_scope(ACME):
        overview = await tenant_api.get_overview(db, session_of(ACME))
    assert [(i.period, i.status) for i in overview.invoices] == [
        ("2026-09", "paid"),
        ("2026-08", "sent"),
    ]
    assert [p.tier for p in overview.packages] == [1, 2, 3]
    assert {p.audience for p in overview.packages} == {"bureau"}
    # Tier 3 has no price, so it is not one an account can take by itself.
    assert overview.upgrade_to == [2]
    assert overview.terms_version == billing.TERMS_VERSION


async def test_a_freelancer_sees_the_freelancer_packages(db: AsyncSession) -> None:
    with tenant_scope(SOLO):
        overview = await tenant_api.get_overview(db, session_of(SOLO, TenantPlan.FREELANCER))
    assert {p.audience for p in overview.packages} == {"freelancer"}
    assert overview.packages[1].monthly_price == Decimal("20.00")


def upgrade_request(tier: int = 2, **changes: Any) -> tenant_api.UpgradeRequest:
    fields: dict[str, Any] = {
        "tier": tier,
        "accepted_terms": True,
        "terms_version": billing.TERMS_VERSION,
    }
    fields.update(changes)
    return tenant_api.UpgradeRequest(**fields)


async def test_one_click_moves_the_account_up_at_once_and_makes_room(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    await fill(db, ACME, 50)
    tenant = await tenant_of(db, ACME)
    with bypass_tenant_scope(), pytest.raises(billing.PackageLimitError):
        await billing.ensure_room_for_order(db, tenant)
    with tenant_scope(ACME):
        usage = await tenant_api.upgrade(upgrade_request(), db, session_of(ACME))
    assert (usage.tier, usage.included, usage.blocked, usage.remaining) == (2, 150, False, 100)
    assert audit[-1]["action"] == "billing.package_upgraded"
    assert audit[-1]["after"]["terms_version"] == billing.TERMS_VERSION
    assert (audit[-1]["before"], audit[-1]["after"]["package_tier"]) == ({"package_tier": 1}, 2)
    with bypass_tenant_scope():
        await billing.ensure_room_for_order(db, await tenant_of(db, ACME))


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"accepted_terms": False}, ValidationError),
        ({"terms_version": "1999-01-01"}, ConflictError),
        ({"tier": 1}, ConflictError),
        ({"tier": 3}, ConflictError),
        ({"tier": 9}, NotFoundError),
    ],
)
async def test_the_move_up_is_refused_without_the_terms_or_to_a_package_it_cannot_take(
    db: AsyncSession, changes: dict[str, Any], error: type[Exception]
) -> None:
    with tenant_scope(ACME), pytest.raises(error):
        await tenant_api.upgrade(upgrade_request(**changes), db, session_of(ACME))
    assert (await tenant_of(db, ACME)).package_tier == 1


async def test_nobody_goes_down_by_clicking(db: AsyncSession) -> None:
    with tenant_scope(GLOBEX), pytest.raises(ConflictError, match="bigger"):
        await tenant_api.upgrade(upgrade_request(tier=1), db, session_of(GLOBEX))


# ── The superuser ───────────────────────────────────────────────────────────


async def test_a_superuser_edits_one_audiences_package_and_the_change_is_audited(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    out = await platform_api.edit_package(
        "freelancer",
        2,
        platform_api.PackageUpdate(orders_included=200, monthly_price=Decimal("25.50")),
        db,
        superuser(),
    )
    assert (out.audience, out.tier, out.orders_included, out.monthly_price) == (
        "freelancer",
        2,
        200,
        Decimal("25.50"),
    )
    stored = await db.get(BillingPackage, ("freelancer", 2))
    assert stored is not None
    assert (stored.orders_included, stored.monthly_price_cents) == (200, 2550)
    (entry,) = audit
    assert entry["action"] == "platform.billing_package_edited"
    assert entry["before"]["orders_included"] == 150
    assert entry["after"]["audience"] == "freelancer"
    # The bureau set is untouched.
    assert (await billing.load_packages(db, "bureau"))[2].orders_included == 150


async def test_a_package_can_be_given_no_price_and_a_missing_one_is_refused(
    db: AsyncSession,
) -> None:
    out = await platform_api.edit_package(
        "bureau",
        1,
        platform_api.PackageUpdate(orders_included=50, monthly_price=None),
        db,
        superuser(),
    )
    assert out.monthly_price is None
    for audience, tier in (("bureau", 4), ("tourist", 1)):
        with pytest.raises(NotFoundError):
            await platform_api.edit_package(
                audience, tier, platform_api.PackageUpdate(orders_included=1), db, superuser()
            )


async def test_all_six_packages_can_be_listed_without_counting_anyone(db: AsyncSession) -> None:
    packages = await platform_api.list_packages(db, superuser())
    assert [(p.audience, p.tier, p.orders_included) for p in packages] == [
        ("bureau", 1, 50),
        ("bureau", 2, 150),
        ("bureau", 3, 500),
        ("freelancer", 1, 50),
        ("freelancer", 2, 150),
        ("freelancer", 3, 500),
    ]


async def test_a_superuser_moves_an_account_to_any_package(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    out = await platform_api.set_tenant_package(
        ACME, platform_api.TenantPackageChange(tier=3), db, superuser()
    )
    assert (out.tier, out.previous) == (3, 1)
    assert (await tenant_of(db, ACME)).package_tier == 3
    assert audit[-1]["action"] == "platform.tenant_package_changed"
    audit.clear()
    await platform_api.set_tenant_package(
        ACME, platform_api.TenantPackageChange(tier=3), db, superuser()
    )
    assert audit == []
    with pytest.raises(NotFoundError):
        await platform_api.set_tenant_package(
            99, platform_api.TenantPackageChange(tier=2), db, superuser()
        )


def test_the_only_ways_to_change_a_package_are_the_platform_area_and_the_owners_upgrade() -> None:
    """No other route takes a package: the owner's one is the upgrade, and it only goes up.

    Read from the OpenAPI schema of a real app: the router's own route list holds
    included routers, not routes, and would show nothing.
    """
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    spec = app.openapi()
    found = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            ref = (
                operation.get("requestBody", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
                .get("$ref")
            )
            properties = (
                spec["components"]["schemas"][ref.split("/")[-1]]["properties"] if ref else {}
            )
            if {"tier", "package_tier"} & set(properties):
                found.append((method, path))
    assert sorted(found) == [
        ("post", "/api/v1/billing/upgrade"),
        ("put", "/api/v1/platform/tenants/{tenant_id}/package"),
    ]


async def test_the_billing_view_lists_every_account_with_its_audience(db: AsyncSession) -> None:
    await fill(db, GLOBEX, 12)
    await fill(db, SOLO, 4)
    out = await platform_api.get_billing(db, superuser(), "2026-10")
    assert [(a.tenant_id, a.audience) for a in out.accounts] == [
        (ACME, "bureau"),
        (GLOBEX, "bureau"),
        (SOLO, "freelancer"),
        (PERSONAL, "freelancer"),
    ]
    globex = out.accounts[1]
    assert (globex.usage.tier, globex.usage.orders, globex.usage.included) == (2, 12, 150)
    assert out.accounts[2].usage.orders == 4
    assert out.accounts[3].usage.orders == 0
    with pytest.raises(ValidationError):
        await platform_api.get_billing(db, superuser(), "10/2026")


async def test_invoices_are_prepared_once_for_a_month_that_has_ended(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    await fill(db, ACME, 53, month=7)
    await make_order(db, GLOBEX, when(5, month=7))
    await make_order(db, SOLO, when(5, month=7))
    request = platform_api.PrepareRequest(period="2026-07")
    out = await platform_api.prepare_invoices(request, db, superuser())
    # The freelancer and the personal workspace are on a free package: nothing to invoice.
    assert (out.created, out.existing, out.not_ended, out.nothing_due) == (2, 0, 0, 2)
    with bypass_tenant_scope():
        rows = {r.tenant_id: r for r in (await db.execute(select(BillingInvoice))).scalars().all()}
    acme = rows[ACME]
    assert (acme.orders_counted, acme.plan_fee_cents, acme.total_cents) == (53, 5000, 5000)
    assert acme.status is BillingInvoiceStatus.OPEN
    assert rows[GLOBEX].total_cents == 10000
    assert audit[-1]["action"] == "platform.billing_invoices_prepared"
    again = await platform_api.prepare_invoices(request, db, superuser())
    assert (again.created, again.existing) == (0, 2)


async def test_a_month_that_has_not_ended_is_not_invoiced(db: AsyncSession) -> None:
    current = billing.current_period("Asia/Tbilisi")
    out = await platform_api.prepare_invoices(
        platform_api.PrepareRequest(period=current), db, superuser()
    )
    assert (out.created, out.not_ended) == (0, 4)


async def test_an_invoice_is_frozen_when_prepared_and_a_later_package_edit_leaves_it(
    db: AsyncSession,
) -> None:
    await make_order(db, ACME, when(5, month=7))
    await platform_api.prepare_invoices(
        platform_api.PrepareRequest(period="2026-07"), db, superuser()
    )
    await platform_api.edit_package(
        "bureau",
        1,
        platform_api.PackageUpdate(orders_included=10, monthly_price=Decimal("999")),
        db,
        superuser(),
    )
    with bypass_tenant_scope():
        invoice = (
            await db.execute(select(BillingInvoice).where(BillingInvoice.tenant_id == ACME))
        ).scalar_one()
    assert (invoice.plan_fee_cents, invoice.total_cents) == (5000, 5000)


async def test_an_account_with_nothing_to_pay_gets_no_invoice(db: AsyncSession) -> None:
    await platform_api.edit_package(
        "bureau",
        1,
        platform_api.PackageUpdate(orders_included=50, monthly_price=None),
        db,
        superuser(),
    )
    await make_order(db, ACME, when(5, month=7))
    out = await platform_api.prepare_invoices(
        platform_api.PrepareRequest(period="2026-07"), db, superuser()
    )
    # ACME has no price; GLOBEX (package 2) still owes its fee; the two freelancers are free.
    assert (out.created, out.nothing_due) == (1, 3)


async def test_an_invoice_is_followed_by_hand_open_sent_paid_or_waived(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    await make_order(db, ACME, when(5, month=7))
    await platform_api.prepare_invoices(
        platform_api.PrepareRequest(period="2026-07"), db, superuser()
    )
    with bypass_tenant_scope():
        invoice_id = (
            await db.execute(select(BillingInvoice.id).where(BillingInvoice.tenant_id == ACME))
        ).scalar_one()

    async def change(status: BillingInvoiceStatus, note: str | None = None) -> Any:
        return await platform_api.set_invoice_status(
            invoice_id, platform_api.InvoiceChange(status=status, note=note), db, superuser()
        )

    sent = await change(BillingInvoiceStatus.SENT, "Emailed 3 Aug")
    assert (sent.status, sent.note) == ("sent", "Emailed 3 Aug")
    assert sent.sent_at is not None
    assert sent.paid_at is None
    paid = await change(BillingInvoiceStatus.PAID)
    assert paid.paid_at is not None
    # SQLite hands a time back without its zone; the moment is the same.
    assert paid.sent_at.replace(tzinfo=None) == sent.sent_at.replace(tzinfo=None)
    assert paid.note == "Emailed 3 Aug"
    back = await change(BillingInvoiceStatus.SENT)
    assert back.paid_at is None
    waived = await change(BillingInvoiceStatus.WAIVED, "")
    assert (waived.status, waived.note, waived.paid_at) == ("waived", None, None)
    assert audit[-1]["action"] == "platform.billing_invoice_updated"
    with pytest.raises(NotFoundError):
        await platform_api.set_invoice_status(
            9999, platform_api.InvoiceChange(status=BillingInvoiceStatus.PAID), db, superuser()
        )


@pytest.mark.parametrize(
    ("claimed", "stored"),
    [(None, "office"), ("", "office"), ("website", "website"), ("import", "office")],
)
def test_a_new_order_cannot_claim_to_be_an_import(claimed: str | None, stored: str) -> None:
    assert orders_api.order_source(claimed) == stored


# ── Dates, months and free months ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("period", "first", "last"),
    [
        ("2026-10", date(2026, 10, 1), date(2026, 10, 31)),
        ("2026-12", date(2026, 12, 1), date(2026, 12, 31)),
        ("2028-02", date(2028, 2, 1), date(2028, 2, 29)),
        ("2026-02", date(2026, 2, 1), date(2026, 2, 28)),
    ],
)
def test_a_period_runs_from_the_first_to_the_last_day_of_its_month(
    period: str, first: date, last: date
) -> None:
    assert billing.period_days(period) == (first, last)


@pytest.mark.parametrize(
    ("period", "before"), [("2026-10", "2026-09"), ("2026-01", "2025-12"), ("2026-03", "2026-02")]
)
def test_the_month_before_crosses_the_year(period: str, before: str) -> None:
    assert billing.previous_period(period) == before


async def test_billing_starts_on_the_day_the_account_began_in_its_own_time_zone(
    db: AsyncSession,
) -> None:
    tenant = await tenant_of(db, ACME)
    tenant.created_at = datetime(2026, 8, 31, 21, 0, tzinfo=UTC)  # 01:00 on 1 Sep in Tbilisi
    tenant.timezone = "Asia/Tbilisi"
    assert billing.started_on(tenant) == date(2026, 9, 1)


async def _free(db: AsyncSession, period: str, tenant_id: int = ACME, free: bool = True) -> Any:
    return await platform_api.set_free_month(
        tenant_id, period, platform_api.FreeMonthChange(free=free), db, superuser()
    )


async def test_a_superuser_makes_a_month_free_and_takes_it_back(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    out = await _free(db, "2026-09")
    assert (out.tenant_id, out.period, out.free) == (ACME, "2026-09", True)
    assert audit[-1]["action"] == "platform.billing_free_month_set"
    with bypass_tenant_scope():
        assert await billing.free_periods(db, ACME) == {"2026-09"}
        assert await billing.free_periods(db, GLOBEX) == set()

    # Saying it again changes nothing and is not audited a second time.
    count = len(audit)
    await _free(db, "2026-09")
    assert len(audit) == count
    with bypass_tenant_scope():
        rows = (await db.execute(select(BillingFreeMonth))).scalars().all()
    assert len(rows) == 1

    back = await _free(db, "2026-09", free=False)
    assert back.free is False
    assert audit[-1]["action"] == "platform.billing_free_month_removed"
    with bypass_tenant_scope():
        assert await billing.free_periods(db, ACME) == set()
    # Taking back what was never set is quiet too.
    count = len(audit)
    await _free(db, "2026-09", free=False)
    assert len(audit) == count


async def test_a_free_month_needs_a_real_month_and_a_real_account(db: AsyncSession) -> None:
    with pytest.raises(ValidationError):
        await _free(db, "10/2026")
    with pytest.raises(NotFoundError):
        await _free(db, "2026-09", tenant_id=9999)


async def test_a_month_that_already_has_an_invoice_is_set_to_waived_instead(
    db: AsyncSession,
) -> None:
    await make_order(db, ACME, when(5, month=7))
    await platform_api.prepare_invoices(
        platform_api.PrepareRequest(period="2026-07"), db, superuser()
    )
    with pytest.raises(ConflictError):
        await _free(db, "2026-07")
    with bypass_tenant_scope():
        assert await billing.free_periods(db, ACME) == set()
    # Taking a free mark back is never refused.
    await _free(db, "2026-07", free=False)


async def test_a_free_month_is_free_for_that_account_only_and_gets_no_invoice(
    db: AsyncSession,
) -> None:
    await make_order(db, ACME, when(5, month=7))
    await make_order(db, GLOBEX, when(5, month=7))
    await _free(db, "2026-07", ACME)
    out = await platform_api.prepare_invoices(
        platform_api.PrepareRequest(period="2026-07"), db, superuser()
    )
    # ACME is free; GLOBEX still owes; the freelancer and personal workspace owe nothing anyway.
    assert (out.created, out.nothing_due) == (1, 3)
    with bypass_tenant_scope():
        owed = (await db.execute(select(BillingInvoice.tenant_id))).scalars().all()
    assert list(owed) == [GLOBEX]
    # The next month is not free.
    await make_order(db, ACME, when(5, month=8))
    later = await platform_api.prepare_invoices(
        platform_api.PrepareRequest(period="2026-08"), db, superuser()
    )
    assert later.created == 2


async def test_a_free_month_waives_only_the_fee_the_limit_still_applies(
    db: AsyncSession,
) -> None:
    current = billing.current_period("Asia/Tbilisi")
    await _free(db, current)
    await fill(db, ACME, 50, month=int(current[5:]))
    tenant = await tenant_of(db, ACME)
    with pytest.raises(billing.PackageLimitError), bypass_tenant_scope():
        await billing.ensure_room_for_order(db, tenant)


async def test_the_owners_usage_and_overview_say_the_month_is_free(db: AsyncSession) -> None:
    current = billing.current_period("Asia/Tbilisi")
    with tenant_scope(ACME):
        plain = await tenant_api.tenant_usage(db, await tenant_of(db, ACME))
    assert plain.free_month is False
    await _free(db, current)
    await _free(db, "2026-01")
    with tenant_scope(ACME):
        tenant = await tenant_of(db, ACME)
        usage = await tenant_api.tenant_usage(db, tenant)
        other = await tenant_api.tenant_usage(db, tenant, "2026-01")
        untouched = await tenant_api.tenant_usage(db, tenant, "2026-02")
        overview = await tenant_api.get_overview(db, session_of(ACME))
    assert usage.free_month is True
    assert other.free_month is True
    assert untouched.free_month is False
    with tenant_scope(GLOBEX):
        elsewhere = await tenant_api.tenant_usage(db, await tenant_of(db, GLOBEX))
    assert elsewhere.free_month is False
    assert overview.free_months == sorted([current, "2026-01"], reverse=True)


async def test_an_accounts_page_has_its_dates_and_a_free_flag_for_this_month(
    db: AsyncSession,
) -> None:
    tenant = await tenant_of(db, ACME)
    tenant.created_at = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
    current = billing.current_period("Asia/Tbilisi")
    first, last = billing.period_days(current)

    page = await platform_api.get_tenant_billing(ACME, db, superuser())
    assert (page.tenant_id, page.audience, page.tier) == (ACME, "bureau", 1)
    assert page.billing_started == date(2026, 8, 12)
    assert (page.period, page.period_start, page.period_end) == (current, first, last)
    assert page.free_this_month is False

    await _free(db, current)
    assert (await platform_api.get_tenant_billing(ACME, db, superuser())).free_this_month is True
    with pytest.raises(NotFoundError):
        await platform_api.get_tenant_billing(9999, db, superuser())


async def test_an_accounts_months_run_back_from_now_to_the_month_it_began(
    db: AsyncSession,
) -> None:
    tenant = await tenant_of(db, ACME)
    current = billing.current_period("Asia/Tbilisi")
    # The account began three months before this one.
    start = current
    for _ in range(3):
        start = billing.previous_period(start)
    year, month = billing.parse_period(start)
    tenant.created_at = datetime(year, month, 10, 9, 0, tzinfo=UTC)

    page = await platform_api.get_tenant_billing(ACME, db, superuser())
    periods = [row.period for row in page.months]
    assert periods[0] == current
    assert periods[-1] == start
    assert len(periods) == 4
    assert periods == sorted(periods, reverse=True)


async def test_an_old_account_lists_two_years_and_no_more(db: AsyncSession) -> None:
    tenant = await tenant_of(db, ACME)
    tenant.created_at = datetime(2015, 1, 1, tzinfo=UTC)
    page = await platform_api.get_tenant_billing(ACME, db, superuser())
    assert len(page.months) == platform_api.MONTHS_SHOWN == 24


async def test_each_month_shows_its_orders_its_fee_and_where_it_stands(
    db: AsyncSession,
) -> None:
    tenant = await tenant_of(db, ACME)
    current = billing.current_period("Asia/Tbilisi")
    tenant.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    # One invoiced month, one free month, one plain one, and this month.
    invoiced, free, plain = "2026-07", "2026-06", "2026-05"
    for period, count in ((invoiced, 3), (free, 2), (plain, 1)):
        await fill(db, ACME, count, month=int(period[5:]))
    await platform_api.prepare_invoices(
        platform_api.PrepareRequest(period=invoiced), db, superuser()
    )
    await _free(db, free)
    page = await platform_api.get_tenant_billing(ACME, db, superuser())
    rows = {row.period: row for row in page.months}

    now = rows[current]
    assert (now.state, now.free, now.invoice) == ("current", False, None)
    assert now.fee == Decimal("50.00")
    assert now.included == 50

    done = rows[invoiced]
    assert (done.state, done.orders, done.fee) == ("open", 3, Decimal("50.00"))
    assert done.invoice is not None

    gift = rows[free]
    assert (gift.state, gift.free, gift.orders, gift.fee) == ("free", True, 2, Decimal("0.00"))
    assert gift.invoice is None

    bare = rows[plain]
    assert (bare.state, bare.orders, bare.invoice) == ("not_invoiced", 1, None)


async def test_a_month_with_no_price_shows_no_fee(db: AsyncSession) -> None:
    await platform_api.edit_package(
        "bureau",
        1,
        platform_api.PackageUpdate(orders_included=50, monthly_price=None),
        db,
        superuser(),
    )
    page = await platform_api.get_tenant_billing(ACME, db, superuser())
    assert page.months[0].fee is None


async def test_the_billing_view_flags_the_accounts_whose_month_is_free(db: AsyncSession) -> None:
    await _free(db, "2026-10", GLOBEX)
    out = await platform_api.get_billing(db, superuser(), "2026-10")
    flags = {a.tenant_id: (a.free_month, a.usage.free_month) for a in out.accounts}
    assert flags[GLOBEX] == (True, True)
    assert flags[ACME] == (False, False)
    # Another month is not affected.
    november = await platform_api.get_billing(db, superuser(), "2026-11")
    assert not any(a.free_month for a in november.accounts)


def test_the_free_month_switch_is_a_single_put_in_the_platform_area() -> None:
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    paths = app.openapi()["paths"]
    free = [p for p in paths if "free-months" in p]
    assert free == ["/api/v1/platform/tenants/{tenant_id}/free-months/{period}"]
    assert list(paths[free[0]]) == ["put"]
