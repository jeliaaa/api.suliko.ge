"""An account's order package, this month's usage, and the one-click move up.

Every account is on one of three packages, the same for everyone of its kind
(bureaus have their own set, freelancers theirs; both are set by a superuser in
`platform_billing.py`). When the month's orders reach the package, new orders are
refused until the account moves up. The owner does that here: pick a bigger
package, read a short summary of the terms, confirm, and the package changes at
once. Moving DOWN, or onto a package that has no listed price, is done by Suliko.

Nothing is charged per order. The month's invoice is the package's monthly fee.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from suliko.api.deps import Db, require
from suliko.core.errors import ConflictError, NotFoundError, ValidationError
from suliko.domain.billing import (
    TERMS_VERSION,
    Package,
    Quote,
    audience_of,
    current_period,
    free_periods,
    from_cents,
    load_packages,
    month_of,
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
    audience: str
    tier: int
    orders_included: int
    #: 0 is free. Null: agreed with Suliko, so there is no price to print.
    monthly_price: Decimal | None


class UsageOut(BaseModel):
    """One calendar month of one account, against its package."""

    #: "2026-10", the account's own calendar month.
    period: str
    tier: int
    orders: int
    included: int
    remaining: int
    #: "ok", "near" (80 percent of the included orders used) or "limit" (all of them:
    #: new orders are refused until the account moves up a package).
    level: str
    blocked: bool
    monthly_price: Decimal | None
    #: A superuser made this month free: no fee. The package's order limit still applies.
    free_month: bool = False


class InvoiceOut(BaseModel):
    id: int
    period: str
    tier: int
    status: str
    orders_included: int
    orders_counted: int
    monthly_price: Decimal | None
    total: Decimal
    sent_at: datetime | None
    paid_at: datetime | None
    note: str | None


class BillingOverview(BaseModel):
    usage: UsageOut
    packages: list[PackageOut]
    #: The tiers this account can move to by itself: bigger, and with a price.
    upgrade_to: list[int]
    #: The version of the terms the confirmation is made against; send it back.
    terms_version: str
    invoices: list[InvoiceOut]
    #: Months ("2026-09") made free for this account, listed beside the invoices.
    free_months: list[str]


class UpgradeRequest(BaseModel):
    tier: int
    #: The person ticked the box under the terms summary.
    accepted_terms: bool
    #: The version of the terms the screen showed.
    terms_version: str


# ── Builders, shared with the platform area ─────────────────────────────────


def package_out(package: Package) -> PackageOut:
    return PackageOut(
        audience=package.audience,
        tier=package.tier,
        orders_included=package.orders_included,
        monthly_price=None
        if package.monthly_price_cents is None
        else from_cents(package.monthly_price_cents),
    )


def usage_out(
    *, period: str, package: Package, figures: Quote, free_month: bool = False
) -> UsageOut:
    return UsageOut(
        period=period,
        tier=package.tier,
        orders=figures.orders,
        included=figures.included,
        remaining=figures.remaining,
        level=figures.level.value,
        blocked=figures.blocked,
        monthly_price=None
        if figures.plan_fee_cents is None
        else from_cents(figures.plan_fee_cents),
        free_month=free_month,
    )


def invoice_out(row: BillingInvoice) -> InvoiceOut:
    return InvoiceOut(
        id=row.id,
        period=row.period,
        tier=row.package_tier,
        status=row.status.value,
        orders_included=row.orders_included,
        orders_counted=row.orders_counted,
        monthly_price=None if row.plan_fee_cents is None else from_cents(row.plan_fee_cents),
        total=from_cents(row.total_cents),
        sent_at=row.sent_at,
        paid_at=row.paid_at,
        note=row.note,
    )


async def tenant_usage(db: Db, tenant: Tenant, period: str | None = None) -> UsageOut:
    """This month (or ``period``) for one account."""
    package, figures = await month_of(db, tenant, period)
    chosen = period or current_period(tenant.timezone)
    return usage_out(
        period=chosen,
        package=package,
        figures=figures,
        free_month=chosen in await free_periods(db, tenant.id),
    )


def upgrade_targets(current: int, packages: dict[int, Package]) -> list[int]:
    """Bigger packages that have a price: the ones an account can take by itself."""
    return [
        tier
        for tier, package in sorted(packages.items())
        if tier > current and package.monthly_price_cents is not None
    ]


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
    return await tenant_usage(db, await _own_tenant(db, session))


@router.get("", response_model=BillingOverview)
async def get_overview(db: Db, session: Billing) -> BillingOverview:
    """The Billing page: this month, the packages, and the invoices asked for.

    An invoice a superuser has worked out but not yet asked for (``open``) is not
    shown: it can still change.
    """
    tenant = await _own_tenant(db, session)
    packages = await load_packages(db, audience_of(tenant))
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
        usage=await tenant_usage(db, tenant),
        packages=[package_out(packages[tier]) for tier in sorted(packages)],
        upgrade_to=upgrade_targets(tenant.package_tier, packages),
        terms_version=TERMS_VERSION,
        invoices=[invoice_out(row) for row in rows],
        free_months=sorted(await free_periods(db, tenant.id), reverse=True),
    )


@router.post("/upgrade", response_model=UsageOut)
async def upgrade(payload: UpgradeRequest, db: Db, session: Billing) -> UsageOut:
    """Move up to a bigger package, now.

    The person has seen the terms summary and ticked the box; the version they saw
    must still be the current one. The package changes at once, so the orders they
    were stopped from adding can be added straight away, and the month is invoiced
    at the new fee. Smaller packages and packages with no listed price are not
    taken here: Suliko moves an account there.
    """
    if not payload.accepted_terms:
        raise ValidationError("Confirm the terms to change your package.")
    if payload.terms_version != TERMS_VERSION:
        raise ConflictError("The terms have changed. Reload the page and read them again.")

    tenant = await _own_tenant(db, session)
    packages = await load_packages(db, audience_of(tenant))
    if payload.tier not in packages:
        raise NotFoundError("There is no such package.")
    if payload.tier <= tenant.package_tier:
        raise ConflictError("You can only move to a bigger package here. Write to us to go down.")
    if packages[payload.tier].monthly_price_cents is None:
        raise ConflictError("That package is agreed with us. Write to us and we will set it up.")

    before = tenant.package_tier
    tenant.package_tier = payload.tier
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="billing.package_upgraded",
        entity_type="tenant",
        entity_id=tenant.id,
        before={"package_tier": before},
        after={
            "package_tier": payload.tier,
            "terms_version": payload.terms_version,
            "audience": audience_of(tenant),
        },
    )
    return await tenant_usage(db, tenant)
