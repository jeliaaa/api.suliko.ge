"""Order packages: counting a bureau's month, the warnings, and the invoices.

The arithmetic (`domain/billing.py`) is pure and tested directly. The counting
and the endpoints run for real against in-memory SQLite, with the tenant filter
installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from suliko.api.v1 import billing as tenant_api
from suliko.api.v1 import platform_billing as platform_api
from suliko.api.v1.router import api_router
from suliko.core.errors import NotFoundError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain import billing
from suliko.domain.plans import TenantPlan
from suliko.models.billing import BillingInvoice, BillingInvoiceStatus, BillingPackage
from suliko.models.order import Order, OrderStatusEvent
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role
from suliko.security.permissions import permissions_for_role
from suliko.security.sessions import AuthenticatedSession

install_tenant_filter()

ACME, GLOBEX, SOLO, PERSONAL = 1, 2, 3, 4
PACKAGE_1 = billing.default_package(1)


# ── The arithmetic ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("orders", "level"),
    [
        (0, "ok"),
        (39, "ok"),
        (40, "near"),
        (49, "near"),
        (50, "limit"),
        (51, "limit"),
    ],
)
def test_the_warning_starts_at_eighty_percent_and_again_at_all_of_them(
    orders: int, level: str
) -> None:
    assert billing.level_of(orders, 50) == level


def test_a_small_package_warns_at_the_right_order() -> None:
    # 80 percent of 10 is 8; 80 percent of 7 is 5.6, so six orders is the first past it.
    assert billing.level_of(7, 10) == "ok"
    assert billing.level_of(8, 10) == "near"
    assert billing.level_of(5, 7) == "ok"
    assert billing.level_of(6, 7) == "near"


def test_orders_past_the_included_ones_are_charged_one_lari_each_on_any_package() -> None:
    for tier in billing.TIERS:
        package = billing.default_package(tier)
        under = billing.quote(package, package.orders_included)
        assert (under.extra_orders, under.extra_fee_cents) == (0, 0)
        over = billing.quote(package, package.orders_included + 3)
        assert (over.extra_orders, over.extra_fee_cents) == (3, 300)
        assert over.total_cents == (package.monthly_price_cents or 0) + 300


def test_a_package_with_no_price_bills_only_the_extras() -> None:
    package = billing.default_package(3)
    assert package.monthly_price_cents is None
    figures = billing.quote(package, package.orders_included + 2)
    assert (figures.plan_fee_cents, figures.total_cents) == (None, 200)
    assert billing.quote(package, 10).total_cents == 0


def test_lari_and_tetri_round_trip_and_round_half_up() -> None:
    assert billing.to_cents(Decimal("50")) == 5000
    assert billing.to_cents(Decimal("0.005")) == 1
    assert billing.to_cents(Decimal("12.345")) == 1235
    assert billing.from_cents(5000) == Decimal("50.00")
    assert str(billing.from_cents(5)) == "0.05"


def test_a_month_is_the_bureaus_own_calendar_month() -> None:
    start, end = billing.month_window("2026-10", "Asia/Tbilisi")
    # Tbilisi is UTC+4: October starts at 20:00 UTC on the 30th of September.
    assert start == datetime(2026, 9, 30, 20, tzinfo=UTC)
    assert end == datetime(2026, 10, 31, 20, tzinfo=UTC)
    assert billing.month_window("2026-12", "Asia/Tbilisi")[1] == datetime(
        2026, 12, 31, 20, tzinfo=UTC
    )
    # Half past midnight on the 1st in Tbilisi is still the 31st in UTC.
    night = datetime(2026, 10, 31, 20, 30, tzinfo=UTC)
    assert billing.current_period("Asia/Tbilisi", now=night) == "2026-11"
    assert billing.current_period("UTC", now=night) == "2026-10"


@pytest.mark.parametrize("bad", ["", "2026", "2026-13", "2026-1", "26-10", "october"])
def test_a_month_must_be_written_year_dash_month(bad: str) -> None:
    with pytest.raises(ValidationError):
        billing.parse_period(bad)


# ── Counting ────────────────────────────────────────────────────────────────

MODELS = (Tenant, Order, OrderStatusEvent, BillingPackage, BillingInvoice)


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


async def counted(db: AsyncSession, tenant_id: int = ACME, period: str = "2026-10") -> int:
    with bypass_tenant_scope():
        return await billing.count_orders(db, tenant_id, period, "Asia/Tbilisi")


async def test_only_orders_made_in_the_bureaus_calendar_month_are_counted(db: AsyncSession) -> None:
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


async def test_each_bureau_counts_only_its_own_orders(db: AsyncSession) -> None:
    await make_order(db, ACME, when(2))
    await make_order(db, GLOBEX, when(3))
    await make_order(db, GLOBEX, when(4))
    assert (await counted(db, ACME), await counted(db, GLOBEX)) == (1, 2)


async def test_only_a_bureau_is_counted_not_a_freelancer_a_personal_account_or_a_new_signup(
    db: AsyncSession,
) -> None:
    with bypass_tenant_scope():
        by_id = {t.id: t for t in (await db.execute(select(Tenant))).scalars()}
    assert billing.is_counted(by_id[ACME]) is True
    assert billing.is_counted(by_id[SOLO]) is False
    assert billing.is_counted(by_id[PERSONAL]) is False
    by_id[ACME].plan = None
    assert billing.is_counted(by_id[ACME]) is False


# ── The bureau's own view ───────────────────────────────────────────────────


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


async def test_usage_says_how_far_into_the_package_the_bureau_is(db: AsyncSession) -> None:
    for day in range(1, 42):
        await make_order(db, ACME, when(1 + day % 28, hour=day % 24))
    # The orders above are in October 2026; ask for that month explicitly.
    with tenant_scope(ACME):
        tenant = await db.get(Tenant, ACME)
        usage = await tenant_api.tenant_usage(
            db, tenant, await billing.load_packages(db), "2026-10"
        )  # type: ignore[arg-type]
    assert (usage.counted, usage.orders, usage.included, usage.level) == (True, 41, 50, "near")
    assert (usage.extra_orders, usage.extra_fee, usage.total) == (
        0,
        Decimal("0.00"),
        Decimal("50.00"),
    )
    assert usage.plan_fee == Decimal("50.00")


async def test_past_the_package_each_extra_order_adds_a_lari(db: AsyncSession) -> None:
    for number in range(53):
        await make_order(db, ACME, when(1 + number % 28, hour=number % 24))
    with tenant_scope(ACME):
        tenant = await db.get(Tenant, ACME)
        usage = await tenant_api.tenant_usage(
            db, tenant, await billing.load_packages(db), "2026-10"
        )  # type: ignore[arg-type]
    assert (usage.level, usage.extra_orders, usage.extra_fee) == ("limit", 3, Decimal("3.00"))
    assert usage.total == Decimal("53.00")


async def test_a_freelancer_is_not_counted_or_limited(db: AsyncSession) -> None:
    for number in range(60):
        await make_order(db, SOLO, when(1 + number % 28, hour=number % 24))
    with tenant_scope(SOLO):
        tenant = await db.get(Tenant, SOLO)
        usage = await tenant_api.tenant_usage(
            db, tenant, await billing.load_packages(db), "2026-10"
        )  # type: ignore[arg-type]
    assert (usage.counted, usage.orders, usage.level, usage.extra_fee) == (
        False,
        0,
        "ok",
        Decimal("0.00"),
    )


async def test_the_packages_fall_back_to_the_defaults_when_none_are_stored(
    db: AsyncSession,
) -> None:
    packages = await billing.load_packages(db)
    assert {tier: p.orders_included for tier, p in packages.items()} == {1: 50, 2: 150, 3: 500}
    assert packages[3].monthly_price_cents is None


async def test_the_overview_lists_packages_and_only_invoices_already_asked_for(
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
                    extra_orders=2,
                    plan_fee_cents=5000,
                    extra_fee_cents=200,
                    total_cents=5200,
                    status=status,
                )
            )
        db.add(
            BillingInvoice(
                tenant_id=GLOBEX,
                period="2026-09",
                package_tier=2,
                orders_included=150,
                orders_counted=10,
                extra_orders=0,
                plan_fee_cents=10000,
                extra_fee_cents=0,
                total_cents=10000,
                status=BillingInvoiceStatus.SENT,
            )
        )
        await db.flush()
    with tenant_scope(ACME):
        overview = await tenant_api.get_overview(db, session_of(ACME))
    assert [(i.period, i.status) for i in overview.invoices] == [
        ("2026-09", "paid"),
        ("2026-08", "sent"),
    ]
    assert overview.invoices[0].total == Decimal("52.00")
    assert [p.tier for p in overview.packages] == [1, 2, 3]
    assert overview.packages[0].monthly_price == Decimal("50.00")
    assert overview.packages[2].monthly_price is None


# ── The superuser ───────────────────────────────────────────────────────────


async def test_a_superuser_edits_a_package_and_the_change_is_audited(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    out = await platform_api.edit_package(
        2,
        platform_api.PackageUpdate(
            orders_included=200, monthly_price=Decimal("120.50"), extra_order_price=Decimal("1.50")
        ),
        db,
        superuser(),
    )
    assert (out.tier, out.orders_included, out.monthly_price, out.extra_order_price) == (
        2,
        200,
        Decimal("120.50"),
        Decimal("1.50"),
    )
    stored = await db.get(BillingPackage, 2)
    assert stored is not None
    assert (stored.orders_included, stored.monthly_price_cents, stored.extra_order_cents) == (
        200,
        12050,
        150,
    )
    (entry,) = audit
    assert entry["action"] == "platform.billing_package_edited"
    assert entry["before"]["orders_included"] == 150
    assert entry["after"]["orders_included"] == 200
    # And it is what the bureaus see, and what the next month is billed by.
    packages = await billing.load_packages(db)
    assert billing.quote(packages[2], 203).extra_fee_cents == 450


async def test_the_packages_can_be_listed_without_counting_anyone(db: AsyncSession) -> None:
    packages = await platform_api.list_packages(db, superuser())
    assert [(p.tier, p.orders_included) for p in packages] == [(1, 50), (2, 150), (3, 500)]


async def test_a_package_can_have_no_price_and_a_package_that_does_not_exist_is_refused(
    db: AsyncSession,
) -> None:
    out = await platform_api.edit_package(
        1,
        platform_api.PackageUpdate(
            orders_included=50, monthly_price=None, extra_order_price=Decimal("1")
        ),
        db,
        superuser(),
    )
    assert out.monthly_price is None
    with pytest.raises(NotFoundError):
        await platform_api.edit_package(
            4,
            platform_api.PackageUpdate(orders_included=1, extra_order_price=Decimal("1")),
            db,
            superuser(),
        )


async def test_a_superuser_moves_a_bureau_to_another_package(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    out = await platform_api.set_tenant_package(
        ACME, platform_api.TenantPackageChange(tier=3), db, superuser()
    )
    assert (out.tier, out.previous) == (3, 1)
    with bypass_tenant_scope():
        assert (await db.get(Tenant, ACME)).package_tier == 3  # type: ignore[union-attr]
    assert audit[-1]["action"] == "platform.tenant_package_changed"
    assert audit[-1]["before"] == {"package_tier": 1}
    # Choosing the package it is already on writes nothing.
    audit.clear()
    await platform_api.set_tenant_package(
        ACME, platform_api.TenantPackageChange(tier=3), db, superuser()
    )
    assert audit == []
    with pytest.raises(NotFoundError):
        await platform_api.set_tenant_package(
            99, platform_api.TenantPackageChange(tier=2), db, superuser()
        )


def test_no_package_can_be_chosen_anywhere_but_the_platform_area() -> None:
    """The bureau's own routes never take a package: only a superuser changes one."""
    offenders = []
    for route in api_router.routes:
        body = getattr(route, "body_field", None)
        model = getattr(getattr(body, "type_", None), "model_fields", None)
        if model and {"tier", "package_tier"} & set(model) and "/platform/" not in route.path:  # type: ignore[attr-defined]
            offenders.append(route.path)  # type: ignore[attr-defined]
    assert offenders == []


async def test_the_billing_view_lists_every_bureau_and_leaves_out_freelancers(
    db: AsyncSession,
) -> None:
    for number in range(12):
        await make_order(db, GLOBEX, when(1 + number, hour=number))
    out = await platform_api.get_billing(db, superuser(), "2026-10")
    assert [b.tenant_id for b in out.bureaus] == [ACME, GLOBEX]
    globex = out.bureaus[1]
    assert (globex.usage.tier, globex.usage.orders, globex.usage.included) == (2, 12, 150)
    assert globex.invoice is None
    with pytest.raises(ValidationError):
        await platform_api.get_billing(db, superuser(), "10/2026")


async def test_invoices_are_prepared_once_for_a_month_that_has_ended(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    for number in range(53):
        await make_order(db, ACME, when(1 + number % 28, hour=number % 24, month=7))
    await make_order(db, GLOBEX, when(5, month=7))
    request = platform_api.PrepareRequest(period="2026-07")
    out = await platform_api.prepare_invoices(request, db, superuser())
    assert (out.created, out.existing, out.not_ended, out.nothing_due) == (2, 0, 0, 0)
    with bypass_tenant_scope():
        rows = {r.tenant_id: r for r in (await db.execute(select(BillingInvoice))).scalars().all()}
    acme = rows[ACME]
    assert (acme.orders_counted, acme.extra_orders, acme.extra_fee_cents) == (53, 3, 300)
    assert (acme.plan_fee_cents, acme.total_cents, acme.status) == (
        5000,
        5300,
        BillingInvoiceStatus.OPEN,
    )
    # A bureau with one order still owes its monthly fee.
    assert rows[GLOBEX].total_cents == 10000
    assert audit[-1]["action"] == "platform.billing_invoices_prepared"

    again = await platform_api.prepare_invoices(request, db, superuser())
    assert (again.created, again.existing) == (0, 2)


async def test_a_month_that_has_not_ended_is_not_invoiced(db: AsyncSession) -> None:
    current = billing.current_period("Asia/Tbilisi")
    out = await platform_api.prepare_invoices(
        platform_api.PrepareRequest(period=current), db, superuser()
    )
    assert (out.created, out.not_ended) == (0, 2)


async def test_an_invoice_is_frozen_when_prepared_and_a_later_package_edit_leaves_it(
    db: AsyncSession,
) -> None:
    await make_order(db, ACME, when(5, month=7))
    await platform_api.prepare_invoices(
        platform_api.PrepareRequest(period="2026-07"), db, superuser()
    )
    await platform_api.edit_package(
        1,
        platform_api.PackageUpdate(
            orders_included=10, monthly_price=Decimal("999"), extra_order_price=Decimal("9")
        ),
        db,
        superuser(),
    )
    with bypass_tenant_scope():
        invoice = (
            await db.execute(select(BillingInvoice).where(BillingInvoice.tenant_id == ACME))
        ).scalar_one()
    assert (invoice.plan_fee_cents, invoice.total_cents) == (5000, 5000)


async def test_a_bureau_with_nothing_to_pay_gets_no_invoice(db: AsyncSession) -> None:
    await platform_api.edit_package(
        1,
        platform_api.PackageUpdate(
            orders_included=50, monthly_price=None, extra_order_price=Decimal("1")
        ),
        db,
        superuser(),
    )
    await make_order(db, ACME, when(5, month=7))
    out = await platform_api.prepare_invoices(
        platform_api.PrepareRequest(period="2026-07"), db, superuser()
    )
    # ACME has no price and no extras; GLOBEX (package 2) still owes its fee.
    assert (out.created, out.nothing_due) == (1, 1)


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
    # Moving a paid invoice back takes the paid time away.
    back = await change(BillingInvoiceStatus.SENT)
    assert back.paid_at is None
    waived = await change(BillingInvoiceStatus.WAIVED, "")
    assert (waived.status, waived.note, waived.paid_at) == ("waived", None, None)
    assert audit[-1]["action"] == "platform.billing_invoice_updated"
    with pytest.raises(NotFoundError):
        await platform_api.set_invoice_status(
            9999, platform_api.InvoiceChange(status=BillingInvoiceStatus.PAID), db, superuser()
        )
