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

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

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
from suliko.core.errors import NotFoundError
from suliko.db.session import bind_tenant_guc
from suliko.db.tenancy import bypass_tenant_scope, tenant_scope
from suliko.domain.billing import (
    TIERS,
    Package,
    count_orders,
    current_period,
    is_counted,
    load_packages,
    parse_period,
    quote,
    to_cents,
)
from suliko.models.billing import BillingInvoice, BillingInvoiceStatus, BillingPackage
from suliko.models.tenant import DEFAULT_TIMEZONE, Tenant, TenantStatus

router = APIRouter(prefix="/platform", tags=["platform"])


# ── Schemas ─────────────────────────────────────────────────────────────────


class PackageUpdate(BaseModel):
    orders_included: Annotated[int, Field(ge=1, le=100_000)]
    #: Null: no price, agreed with each bureau.
    monthly_price: Annotated[Decimal, Field(ge=0, le=Decimal("1000000"))] | None = None
    extra_order_price: Annotated[Decimal, Field(ge=0, le=Decimal("10000"))]


class TenantPackageChange(BaseModel):
    tier: Annotated[int, Field(ge=min(TIERS), le=max(TIERS))]


class TenantPackageOut(BaseModel):
    tenant_id: int
    tier: int
    previous: int


class BureauBilling(BaseModel):
    tenant_id: int
    name: str
    slug: str
    status: TenantStatus
    usage: UsageOut
    #: The invoice for this month, if one has been prepared.
    invoice: InvoiceOut | None


class PlatformBilling(BaseModel):
    period: str
    packages: list[PackageOut]
    bureaus: list[BureauBilling]


class PrepareRequest(BaseModel):
    period: Annotated[str, Field(min_length=7, max_length=7)]


class PrepareOut(BaseModel):
    period: str
    #: Invoices written now.
    created: int
    #: Bureaus that already had one for the month, left as they are.
    existing: int
    #: Bureaus whose own calendar month has not ended yet.
    not_ended: int
    #: Bureaus with nothing to pay for the month.
    nothing_due: int


class InvoiceChange(BaseModel):
    status: BillingInvoiceStatus
    note: Annotated[str, Field(max_length=500)] | None = None


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _bureaus(db: Db) -> list[Tenant]:
    """Every tenant the count applies to: bureaus, not freelancers or personal workspaces."""
    with bypass_tenant_scope():
        rows = (
            (
                await db.execute(
                    select(Tenant).where(Tenant.is_personal.is_(False)).order_by(Tenant.id)
                )
            )
            .scalars()
            .all()
        )
    return [tenant for tenant in rows if is_counted(tenant)]


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
    """The packages, and for one month every bureau's count against its package."""
    chosen = period or current_period(DEFAULT_TIMEZONE)
    parse_period(chosen)
    packages = await load_packages(db)
    invoices = await _invoices_for(db, chosen)
    bureaus = []
    for tenant in await _bureaus(db):
        package = packages.get(tenant.package_tier) or packages[1]
        with bypass_tenant_scope():
            orders = await count_orders(db, tenant.id, chosen, tenant.timezone)
        invoice = invoices.get(tenant.id)
        bureaus.append(
            BureauBilling(
                tenant_id=tenant.id,
                name=tenant.display_name,
                slug=tenant.slug,
                status=tenant.status,
                usage=usage_out(
                    counted=True, period=chosen, package=package, figures=quote(package, orders)
                ),
                invoice=invoice_out(invoice) if invoice else None,
            )
        )
    return PlatformBilling(
        period=chosen,
        packages=[package_out(packages[tier]) for tier in sorted(packages)],
        bureaus=bureaus,
    )


@router.get("/billing/packages", response_model=list[PackageOut])
async def list_packages(db: Db, _: Superuser) -> list[PackageOut]:
    """The three packages, without counting anyone's orders: for a bureau's own page."""
    packages = await load_packages(db)
    return [package_out(packages[tier]) for tier in sorted(packages)]


@router.put("/billing/packages/{tier}", response_model=PackageOut)
async def edit_package(tier: int, payload: PackageUpdate, db: Db, session: Superuser) -> PackageOut:
    """Change what a package covers and costs, for every bureau on it.

    Invoices already prepared keep the numbers they were written with.
    """
    if tier not in TIERS:
        raise NotFoundError("There is no such package.")
    row = await db.get(BillingPackage, tier)
    before = (await load_packages(db))[tier]
    monthly = None if payload.monthly_price is None else to_cents(payload.monthly_price)
    if row is None:
        row = BillingPackage(tier=tier, orders_included=1, extra_order_cents=0)
        db.add(row)
    row.orders_included = payload.orders_included
    row.monthly_price_cents = monthly
    row.extra_order_cents = to_cents(payload.extra_order_price)
    await db.flush()

    from suliko.core.audit import record

    after = Package(tier, row.orders_included, row.monthly_price_cents, row.extra_order_cents)
    await record(
        db,
        session,
        action="platform.billing_package_edited",
        entity_type="billing_package",
        entity_id=tier,
        before={
            "orders_included": before.orders_included,
            "monthly_price_cents": before.monthly_price_cents,
            "extra_order_cents": before.extra_order_cents,
        },
        after={
            "orders_included": after.orders_included,
            "monthly_price_cents": after.monthly_price_cents,
            "extra_order_cents": after.extra_order_cents,
        },
    )
    return package_out(after)


@router.put("/tenants/{tenant_id}/package", response_model=TenantPackageOut)
async def set_tenant_package(
    tenant_id: int, payload: TenantPackageChange, db: Db, session: Superuser
) -> TenantPackageOut:
    """Move a bureau to another package. Only a superuser can, whichever package it is on."""
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


# ── Invoices ────────────────────────────────────────────────────────────────


@router.post("/billing/invoices", response_model=PrepareOut)
async def prepare_invoices(payload: PrepareRequest, db: Db, session: Superuser) -> PrepareOut:
    """Work out the invoice of every bureau for a month that has ended.

    Safe to run again: a bureau that already has one for the month is left
    alone, so a bureau that signed up late, or one whose month had not ended,
    can be picked up later without touching the rest.
    """
    period = payload.period
    parse_period(period)
    packages = await load_packages(db)
    existing_for = await _invoices_for(db, period)

    created = existing = not_ended = nothing_due = 0
    for tenant in await _bureaus(db):
        if tenant.id in existing_for:
            existing += 1
            continue
        if period >= current_period(tenant.timezone):
            not_ended += 1
            continue
        package = packages.get(tenant.package_tier) or packages[1]
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
                    extra_orders=figures.extra_orders,
                    plan_fee_cents=figures.plan_fee_cents,
                    extra_fee_cents=figures.extra_fee_cents,
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
