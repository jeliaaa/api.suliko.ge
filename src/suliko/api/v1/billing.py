"""A bureau's order package, this month's usage, and what it has been asked to pay.

Read-only for the bureau. Which package a bureau is on, what each package covers
and costs, and the invoices are all set by a superuser (`platform_billing.py`);
the owner sees them here, on the Billing page, with the figure and the warning
the rest of the app shows.

Nothing in this module, and nothing anywhere, stops an order being made: past the
included orders the bureau is told and each extra one is added to the month's
fee (`domain/billing.py`).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from suliko.api.deps import Db, require
from suliko.core.errors import NotFoundError
from suliko.domain.billing import (
    Package,
    Quote,
    count_orders,
    current_period,
    from_cents,
    is_counted,
    load_packages,
    quote,
)
from suliko.models.billing import BillingInvoice, BillingInvoiceStatus
from suliko.models.tenant import Tenant
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

router = APIRouter(prefix="/billing", tags=["billing"])

Billing = Annotated[AuthenticatedSession, Depends(require(Permission.TENANT_BILLING))]

#: Invoices listed on the Billing page: two years of months.
INVOICES_SHOWN = 24


# ── Schemas ─────────────────────────────────────────────────────────────────


class PackageOut(BaseModel):
    tier: int
    orders_included: int
    #: Null: agreed with each bureau, so there is no price to print.
    monthly_price: Decimal | None
    #: What each order beyond the included ones costs.
    extra_order_price: Decimal


class UsageOut(BaseModel):
    """One calendar month of one bureau, against its package."""

    #: False for a freelancer or a personal account: nothing is counted or limited.
    counted: bool
    #: "2026-10", the bureau's own calendar month.
    period: str
    tier: int
    orders: int
    included: int
    extra_orders: int
    #: "ok", "near" (80 percent of the included orders used) or "limit" (all of them).
    level: str
    plan_fee: Decimal | None
    extra_order_price: Decimal
    extra_fee: Decimal
    #: The fee (when it has a price) plus the extras.
    total: Decimal


class InvoiceOut(BaseModel):
    id: int
    period: str
    tier: int
    status: str
    orders_included: int
    orders_counted: int
    extra_orders: int
    plan_fee: Decimal | None
    extra_fee: Decimal
    total: Decimal
    sent_at: datetime | None
    paid_at: datetime | None
    note: str | None


class BillingOverview(BaseModel):
    usage: UsageOut
    packages: list[PackageOut]
    invoices: list[InvoiceOut]


# ── Builders, shared with the platform area ─────────────────────────────────


def package_out(package: Package) -> PackageOut:
    return PackageOut(
        tier=package.tier,
        orders_included=package.orders_included,
        monthly_price=None
        if package.monthly_price_cents is None
        else from_cents(package.monthly_price_cents),
        extra_order_price=from_cents(package.extra_order_cents),
    )


def usage_out(*, counted: bool, period: str, package: Package, figures: Quote) -> UsageOut:
    return UsageOut(
        counted=counted,
        period=period,
        tier=package.tier,
        orders=figures.orders,
        included=figures.included,
        extra_orders=figures.extra_orders,
        level=figures.level.value,
        plan_fee=None if figures.plan_fee_cents is None else from_cents(figures.plan_fee_cents),
        extra_order_price=from_cents(package.extra_order_cents),
        extra_fee=from_cents(figures.extra_fee_cents),
        total=from_cents(figures.total_cents),
    )


def invoice_out(row: BillingInvoice) -> InvoiceOut:
    return InvoiceOut(
        id=row.id,
        period=row.period,
        tier=row.package_tier,
        status=row.status.value,
        orders_included=row.orders_included,
        orders_counted=row.orders_counted,
        extra_orders=row.extra_orders,
        plan_fee=None if row.plan_fee_cents is None else from_cents(row.plan_fee_cents),
        extra_fee=from_cents(row.extra_fee_cents),
        total=from_cents(row.total_cents),
        sent_at=row.sent_at,
        paid_at=row.paid_at,
        note=row.note,
    )


async def tenant_usage(
    db: Db, tenant: Tenant, packages: dict[int, Package], period: str | None = None
) -> UsageOut:
    """This month (or ``period``) for one bureau. A freelancer is not counted."""
    period = period or current_period(tenant.timezone)
    package = packages.get(tenant.package_tier) or packages[1]
    counted = is_counted(tenant)
    orders = await count_orders(db, tenant.id, period, tenant.timezone) if counted else 0
    return usage_out(
        counted=counted, period=period, package=package, figures=quote(package, orders)
    )


# ── Routes ──────────────────────────────────────────────────────────────────


async def _own_tenant(db: Db, session: AuthenticatedSession) -> Tenant:
    tenant = await db.get(Tenant, session.tenant_id)
    if tenant is None:
        raise NotFoundError("Tenant not found.")
    return tenant


@router.get("/usage", response_model=UsageOut)
async def get_usage(db: Db, session: Billing) -> UsageOut:
    """This month's orders against the package: the figure in the sidebar and the
    warning banner. Cheap enough to ask on every page."""
    tenant = await _own_tenant(db, session)
    return await tenant_usage(db, tenant, await load_packages(db))


@router.get("", response_model=BillingOverview)
async def get_overview(db: Db, session: Billing) -> BillingOverview:
    """The Billing page: this month, the packages, and the invoices asked of the bureau.

    An invoice a superuser has worked out but not yet asked for (``open``) is not
    shown: it can still change.
    """
    tenant = await _own_tenant(db, session)
    packages = await load_packages(db)
    rows = (
        (
            await db.execute(
                select(BillingInvoice)
                .where(
                    BillingInvoice.tenant_id == tenant.id,
                    BillingInvoice.status != BillingInvoiceStatus.OPEN,
                )
                .order_by(BillingInvoice.period.desc())
                .limit(INVOICES_SHOWN)
            )
        )
        .scalars()
        .all()
    )
    return BillingOverview(
        usage=await tenant_usage(db, tenant, packages),
        packages=[package_out(packages[tier]) for tier in sorted(packages)],
        invoices=[invoice_out(row) for row in rows],
    )
