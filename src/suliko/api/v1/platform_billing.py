"""Order packages and invoices, from the platform area.

Superuser-only, and the only place a bureau's package changes, the three packages
are edited, and invoices are worked out and followed. The same rule as
`platform.py` holds: every statement here carries an explicit ``tenant_id``, and a
write into a bureau binds that bureau's id for row-level security and the ORM
stamp together.

## How a month is billed, for now

By hand, by bank transfer (decided 2026-10-15). At the start of a month a
superuser prepares the invoices for the month that ended: one row per bureau,
frozen with the package, the count and the amounts. They then ask each bureau to
pay (outside the app), mark the row ``sent``, and mark it ``paid`` when the money
arrives. Moving a bureau to another package is the "activation". Nothing here
touches a bank; when the bureaus are enough to need it, a payment gateway can
set the same statuses.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import distinct, select

from suliko.api.deps import Db
from suliko.api.v1.billing import (
    InvoiceOut,
    PackageOut,
    UsageOut,
    invoice_out,
    package_out,
    usage_out,
)
from suliko.api.v1.platform import Superuser, _tenant_or_404
from suliko.core.errors import ConflictError, NotFoundError
from suliko.db.session import bind_tenant_guc
from suliko.db.tenancy import bypass_tenant_scope, tenant_scope
from suliko.domain.billing import (
    AUDIENCES,
    TIERS,
    Package,
    audience_of,
    count_orders,
    current_period,
    free_periods,
    from_cents,
    load_packages,
    month_window,
    parse_period,
    period_days,
    previous_period,
    quote,
    started_on,
    to_cents,
)
from suliko.models.billing import (
    BillingFreeMonth,
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPackage,
)
from suliko.models.order import Order
from suliko.models.tenant import DEFAULT_TIMEZONE, Tenant, TenantStatus

router = APIRouter(prefix="/platform", tags=["platform"])


# ── Schemas ─────────────────────────────────────────────────────────────────


class PackageUpdate(BaseModel):
    orders_included: Annotated[int, Field(ge=1, le=100_000)]
    #: 0 is free. Null: no price, agreed with each account.
    monthly_price: Annotated[Decimal, Field(ge=0, le=Decimal("1000000"))] | None = None


class TenantPackageChange(BaseModel):
    tier: Annotated[int, Field(ge=min(TIERS), le=max(TIERS))]


class TenantPackageOut(BaseModel):
    tenant_id: int
    tier: int
    previous: int


class AccountBilling(BaseModel):
    tenant_id: int
    name: str
    slug: str
    #: "bureau" or "freelancer" (a freelancer's or a person's own workspace).
    audience: str
    status: TenantStatus
    usage: UsageOut
    #: This month was made free for the account.
    free_month: bool = False
    #: The invoice for this month, if one has been prepared.
    invoice: InvoiceOut | None


class PlatformBilling(BaseModel):
    period: str
    #: All six: the bureau set and the freelancer set.
    packages: list[PackageOut]
    accounts: list[AccountBilling]


class PrepareRequest(BaseModel):
    period: Annotated[str, Field(min_length=7, max_length=7)]


class PrepareOut(BaseModel):
    period: str
    #: Invoices written now.
    created: int
    #: Accounts that already had one for the month, left as they are.
    existing: int
    #: Accounts whose own calendar month has not ended yet.
    not_ended: int
    #: Accounts with nothing to pay for the month (a free package, or no price).
    nothing_due: int


class MonthRow(BaseModel):
    """One calendar month of one account, as the account's page lists it."""

    period: str
    orders: int
    included: int
    #: What the month costs: the invoice's total when there is one, else the package's fee.
    fee: Decimal | None
    free: bool
    #: "free", "current", "not_invoiced", or the invoice's own status.
    state: str
    invoice: InvoiceOut | None


class TenantBilling(BaseModel):
    """One account's billing: where it starts, this period, and every month so far."""

    tenant_id: int
    audience: str
    tier: int
    #: The day the account began.
    billing_started: date
    #: The current period is the account's own calendar month.
    period: str
    period_start: date
    period_end: date
    free_this_month: bool
    months: list[MonthRow]


class FreeMonthChange(BaseModel):
    free: bool
    note: Annotated[str, Field(max_length=255)] | None = None


class FreeMonthOut(BaseModel):
    tenant_id: int
    period: str
    free: bool


class InvoiceChange(BaseModel):
    status: BillingInvoiceStatus
    note: Annotated[str, Field(max_length=500)] | None = None


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _accounts(db: Db) -> list[Tenant]:
    """Every tenant: bureaus, freelancers and personal workspaces all have a package."""
    with bypass_tenant_scope():
        return list((await db.execute(select(Tenant).order_by(Tenant.id))).scalars().all())


async def _all_packages(db: Db) -> dict[str, dict[int, Package]]:
    return {audience: await load_packages(db, audience) for audience in AUDIENCES}


async def _counts(db: Db, tenants: list[Tenant], period: str) -> dict[int, int]:
    """Each account's orders for a month. One query finds who made any at all, within
    a window wide enough for every time zone; only those are counted exactly."""
    start, end = month_window(period, "UTC")
    wide = (start - timedelta(hours=14), end + timedelta(hours=14))
    with bypass_tenant_scope():
        active = set(
            (
                await db.execute(
                    select(distinct(Order.tenant_id)).where(
                        Order.created_at >= wide[0], Order.created_at < wide[1]
                    )
                )
            )
            .scalars()
            .all()
        )
        return {
            tenant.id: (
                await count_orders(db, tenant.id, period, tenant.timezone)
                if tenant.id in active
                else 0
            )
            for tenant in tenants
        }


async def _free_ids(db: Db, period: str) -> set[int]:
    """The accounts a month was made free for."""
    with bypass_tenant_scope():
        rows = await db.execute(
            select(BillingFreeMonth.tenant_id).where(BillingFreeMonth.period == period)
        )
        return set(rows.scalars().all())


async def _invoices_for(db: Db, period: str) -> dict[int, BillingInvoice]:
    with bypass_tenant_scope():
        rows = (
            (await db.execute(select(BillingInvoice).where(BillingInvoice.period == period)))
            .scalars()
            .all()
        )
    return {row.tenant_id: row for row in rows}


# ── Packages ────────────────────────────────────────────────────────────────


@router.get("/billing", response_model=PlatformBilling)
async def get_billing(db: Db, _: Superuser, period: str | None = None) -> PlatformBilling:
    """The packages, and for one month every account's count against its package."""
    chosen = period or current_period(DEFAULT_TIMEZONE)
    parse_period(chosen)
    packages = await _all_packages(db)
    invoices = await _invoices_for(db, chosen)
    tenants = await _accounts(db)
    counts = await _counts(db, tenants, chosen)
    free_ids = await _free_ids(db, chosen)
    accounts = []
    for tenant in tenants:
        audience = audience_of(tenant)
        package = packages[audience].get(tenant.package_tier) or packages[audience][1]
        invoice = invoices.get(tenant.id)
        accounts.append(
            AccountBilling(
                tenant_id=tenant.id,
                name=tenant.display_name,
                slug=tenant.slug,
                audience=audience,
                status=tenant.status,
                usage=usage_out(
                    period=chosen,
                    package=package,
                    figures=quote(package, counts[tenant.id]),
                    free_month=tenant.id in free_ids,
                ),
                free_month=tenant.id in free_ids,
                invoice=invoice_out(invoice) if invoice else None,
            )
        )
    return PlatformBilling(
        period=chosen,
        packages=[
            package_out(packages[audience][tier]) for audience in AUDIENCES for tier in TIERS
        ],
        accounts=accounts,
    )


@router.get("/billing/packages", response_model=list[PackageOut])
async def list_packages(db: Db, _: Superuser) -> list[PackageOut]:
    """All six packages, without counting anyone's orders: for an account's own page."""
    packages = await _all_packages(db)
    return [package_out(packages[audience][tier]) for audience in AUDIENCES for tier in TIERS]


@router.put("/billing/packages/{audience}/{tier}", response_model=PackageOut)
async def edit_package(
    audience: str, tier: int, payload: PackageUpdate, db: Db, session: Superuser
) -> PackageOut:
    """Change what a package covers and costs, for every account on it.

    Invoices already prepared keep the numbers they were written with.
    """
    if tier not in TIERS or audience not in AUDIENCES:
        raise NotFoundError("There is no such package.")
    row = await db.get(BillingPackage, (audience, tier))
    before = (await load_packages(db, audience))[tier]
    monthly = None if payload.monthly_price is None else to_cents(payload.monthly_price)
    if row is None:
        row = BillingPackage(audience=audience, tier=tier, orders_included=1)
        db.add(row)
    row.orders_included = payload.orders_included
    row.monthly_price_cents = monthly
    await db.flush()

    from suliko.core.audit import record

    after = Package(audience, tier, row.orders_included, row.monthly_price_cents)
    await record(
        db,
        session,
        action="platform.billing_package_edited",
        entity_type="billing_package",
        entity_id=tier,
        before={
            "audience": audience,
            "orders_included": before.orders_included,
            "monthly_price_cents": before.monthly_price_cents,
        },
        after={
            "audience": audience,
            "orders_included": after.orders_included,
            "monthly_price_cents": after.monthly_price_cents,
        },
    )
    return package_out(after)


@router.put("/tenants/{tenant_id}/package", response_model=TenantPackageOut)
async def set_tenant_package(
    tenant_id: int, payload: TenantPackageChange, db: Db, session: Superuser
) -> TenantPackageOut:
    """Move an account to any package, up or down, with or without a listed price.

    An owner can move UP by themselves (`POST /billing/upgrade`); everything else
    is done here.
    """
    tenant = await _tenant_or_404(db, tenant_id)
    previous = tenant.package_tier
    if payload.tier != previous:
        with bypass_tenant_scope():
            tenant.package_tier = payload.tier
            await db.flush()

        from suliko.core.audit import record

        await record(
            db,
            session,
            action="platform.tenant_package_changed",
            entity_type="tenant",
            entity_id=tenant.id,
            tenant_id=tenant.id,
            before={"package_tier": previous},
            after={"package_tier": payload.tier},
        )
    return TenantPackageOut(tenant_id=tenant.id, tier=tenant.package_tier, previous=previous)


# ── One account: its period, its months, its free months ────────────────────

#: Months listed on an account's page: two years back.
MONTHS_SHOWN = 24


def _month_state(period: str, current: str, free: bool, invoice: BillingInvoice | None) -> str:
    if invoice is not None:
        return invoice.status.value
    if free:
        return "free"
    return "current" if period == current else "not_invoiced"


@router.get("/tenants/{tenant_id}/billing", response_model=TenantBilling)
async def get_tenant_billing(tenant_id: int, db: Db, _: Superuser) -> TenantBilling:
    """An account's billing from outside it: the day it began, the current period's
    dates, and every month so far with its orders, its fee and where it stands."""
    tenant = await _tenant_or_404(db, tenant_id)
    audience = audience_of(tenant)
    packages = await load_packages(db, audience)
    package = packages.get(tenant.package_tier) or packages[1]
    current = current_period(tenant.timezone)
    begun = started_on(tenant)
    first = f"{begun.year:04d}-{begun.month:02d}"
    with bypass_tenant_scope():
        free = await free_periods(db, tenant.id)
        invoices = {
            row.period: row
            for row in (
                await db.execute(
                    select(BillingInvoice).where(BillingInvoice.tenant_id == tenant.id)
                )
            )
            .scalars()
            .all()
        }
    months: list[MonthRow] = []
    period = current
    while len(months) < MONTHS_SHOWN:
        with bypass_tenant_scope():
            orders = await count_orders(db, tenant.id, period, tenant.timezone)
        invoice = invoices.get(period)
        is_free = period in free
        if invoice is not None:
            fee: Decimal | None = invoice_out(invoice).total
        elif is_free:
            fee = Decimal("0.00")
        elif package.monthly_price_cents is None:
            fee = None
        else:
            fee = from_cents(package.monthly_price_cents)
        months.append(
            MonthRow(
                period=period,
                orders=orders,
                included=invoice.orders_included if invoice else package.orders_included,
                fee=fee,
                free=is_free,
                state=_month_state(period, current, is_free, invoice),
                invoice=invoice_out(invoice) if invoice else None,
            )
        )
        if period <= first:
            break
        period = previous_period(period)
    start, end = period_days(current)
    return TenantBilling(
        tenant_id=tenant.id,
        audience=audience,
        tier=package.tier,
        billing_started=begun,
        period=current,
        period_start=start,
        period_end=end,
        free_this_month=current in free,
        months=months,
    )


@router.put("/tenants/{tenant_id}/free-months/{period}", response_model=FreeMonthOut)
async def set_free_month(
    tenant_id: int, period: str, payload: FreeMonthChange, db: Db, session: Superuser
) -> FreeMonthOut:
    """Make one whole month free for an account, or take that back.

    Only the fee is waived: the package's order limit still applies. A month that
    already has an invoice is refused: set that invoice to waived instead.
    """
    parse_period(period)
    tenant = await _tenant_or_404(db, tenant_id)
    with bypass_tenant_scope():
        row = (
            await db.execute(
                select(BillingFreeMonth).where(
                    BillingFreeMonth.tenant_id == tenant_id, BillingFreeMonth.period == period
                )
            )
        ).scalar_one_or_none()
        invoiced = (
            await db.execute(
                select(BillingInvoice.id).where(
                    BillingInvoice.tenant_id == tenant_id, BillingInvoice.period == period
                )
            )
        ).scalar_one_or_none()
    if payload.free and invoiced is not None:
        raise ConflictError("That month already has an invoice. Set the invoice to Waived instead.")

    changed = payload.free != (row is not None)
    if changed:
        await bind_tenant_guc(db, tenant.id)
        with tenant_scope(tenant.id):
            if payload.free:
                db.add(
                    BillingFreeMonth(
                        tenant_id=tenant.id,
                        period=period,
                        note=(payload.note or "").strip() or None,
                    )
                )
            elif row is not None:
                await db.delete(row)
            await db.flush()

        from suliko.core.audit import record

        await record(
            db,
            session,
            action="platform.billing_free_month_set"
            if payload.free
            else "platform.billing_free_month_removed",
            entity_type="tenant",
            entity_id=tenant.id,
            tenant_id=tenant.id,
            after={"period": period, "free": payload.free},
        )
    return FreeMonthOut(tenant_id=tenant.id, period=period, free=payload.free)


# ── Invoices ────────────────────────────────────────────────────────────────


@router.post("/billing/invoices", response_model=PrepareOut)
async def prepare_invoices(payload: PrepareRequest, db: Db, session: Superuser) -> PrepareOut:
    """Work out the invoice of every account for a month that has ended.

    Safe to run again: an account that already has one for the month is left
    alone, so one that signed up late, or whose month had not ended, can be
    picked up later without touching the rest. A free package, or one with no
    price, has nothing to invoice.
    """
    period = payload.period
    parse_period(period)
    packages = await _all_packages(db)
    existing_for = await _invoices_for(db, period)
    free_ids = await _free_ids(db, period)

    created = existing = not_ended = nothing_due = 0
    for tenant in await _accounts(db):
        if tenant.id in existing_for:
            existing += 1
            continue
        if period >= current_period(tenant.timezone):
            not_ended += 1
            continue
        if tenant.id in free_ids:
            nothing_due += 1
            continue
        audience = audience_of(tenant)
        package = packages[audience].get(tenant.package_tier) or packages[audience][1]
        with bypass_tenant_scope():
            orders = await count_orders(db, tenant.id, period, tenant.timezone)
        figures = quote(package, orders)
        if figures.total_cents == 0:
            nothing_due += 1
            continue
        await bind_tenant_guc(db, tenant.id)
        with tenant_scope(tenant.id):
            db.add(
                BillingInvoice(
                    tenant_id=tenant.id,
                    period=period,
                    package_tier=package.tier,
                    orders_included=figures.included,
                    orders_counted=figures.orders,
                    plan_fee_cents=figures.plan_fee_cents,
                    total_cents=figures.total_cents,
                    status=BillingInvoiceStatus.OPEN,
                )
            )
            await db.flush()
        created += 1

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="platform.billing_invoices_prepared",
        entity_type="billing_invoice",
        after={
            "period": period,
            "created": created,
            "existing": existing,
            "not_ended": not_ended,
            "nothing_due": nothing_due,
        },
    )
    return PrepareOut(
        period=period,
        created=created,
        existing=existing,
        not_ended=not_ended,
        nothing_due=nothing_due,
    )


@router.put("/billing/invoices/{invoice_id}", response_model=InvoiceOut)
async def set_invoice_status(
    invoice_id: int, payload: InvoiceChange, db: Db, session: Superuser
) -> InvoiceOut:
    """Follow an invoice by hand: open, sent, paid or waived, with a note.

    The times are the moments it was first marked sent and paid; moving a paid
    invoice back clears the paid time.
    """
    with bypass_tenant_scope():
        invoice = await db.get(BillingInvoice, invoice_id)
    if invoice is None:
        raise NotFoundError("Invoice not found.")

    before = {"status": invoice.status.value, "note": invoice.note}
    now = datetime.now(UTC)
    await bind_tenant_guc(db, invoice.tenant_id)
    invoice.status = payload.status
    if payload.status in (BillingInvoiceStatus.SENT, BillingInvoiceStatus.PAID):
        invoice.sent_at = invoice.sent_at or now
    if payload.status is BillingInvoiceStatus.OPEN:
        invoice.sent_at = None
    invoice.paid_at = (
        (invoice.paid_at or now) if payload.status is BillingInvoiceStatus.PAID else None
    )
    if payload.note is not None:
        invoice.note = payload.note.strip() or None
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="platform.billing_invoice_updated",
        entity_type="billing_invoice",
        entity_id=invoice.id,
        tenant_id=invoice.tenant_id,
        before=before,
        after={"status": invoice.status.value, "note": invoice.note, "period": invoice.period},
    )
    return invoice_out(invoice)
