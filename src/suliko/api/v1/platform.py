"""The platform area: every tenant, from outside all of them.

Superuser-only, and the one router in the product that deliberately reads and
writes ACROSS tenants. `api/v1/router.py` deferred it for exactly that reason;
this is the review it was waiting for.

## The rule that makes this safe

Every query here filters on an EXPLICIT `tenant_id` taken from the URL. None
of them relies on the ambient tenant context, and none reuses the
tenant-scoped query helpers in `domain/`.

That is deliberate and it is the whole design. The ORM filter in `db/tenancy`
and the RLS policies both key on "the current tenant", and a router whose job
is to be outside that concept cannot lean on either — so instead of disabling
a safety net and hoping, every statement carries its own predicate. Read any
query below and the tenant it touches is visible in the same expression.

`bypass_tenant_scope()` is therefore held as narrowly as possible: around the
reads, never around a block that also writes something derived from them.

## Writing into someone else's tenant

Three things have to agree, and each write below sets all three together:
`bind_tenant_guc` for the RLS policy, `tenant_scope` for the ORM's insert
stamp, and an explicit `tenant_id` on the row. The tenant is resolved from the
URL and 404s before anything is written, so a write cannot land in a bureau
that does not exist — or, worse, in the operator's own by omission.

## Before row-level security is switched on

Every read here runs under `bypass_tenant_scope()`, which switches off the ORM
filter — but NOT PostgreSQL's RLS policies, which key on the session's GUC.
Today that is invisible because the app connects as a PostgreSQL superuser and
RLS is not enforced at all. The day it is, this router needs its own
connection role with `BYPASSRLS`, or every other tenant's rows will silently
come back empty.

## What is NOT here

**Impersonation.** `Permission.PLATFORM_IMPERSONATE` exists and is in
`STEP_UP_PERMISSIONS`, so it needs a freshly verified second factor — and
there is still no enrolment screen, so the one control standing between "read
a tenant's data" and "act as their owner" cannot currently be satisfied.
Shipping it without that is shipping the feature without its safety catch.

**Creating a superuser.** Unchanged and not negotiable:
`suliko create-superuser` on the server, never over HTTP. An account with
platform-wide reach should require filesystem access to the machine.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import case, func, select

from suliko.api.deps import Db, require
from suliko.core.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from suliko.db.session import bind_tenant_guc
from suliko.db.tenancy import bypass_tenant_scope, tenant_scope
from suliko.domain.plans import TenantPlan, effective_plan
from suliko.models.drive import DriveSettings
from suliko.models.order import Order, OrderDocument
from suliko.models.reference import Language, LanguagePairPrice
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role, User
from suliko.security.passwords import (
    generate_one_time_password,
    hash_password_async,
)
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession, revoke_all_for_user

router = APIRouter(prefix="/platform", tags=["platform"])

Superuser = Annotated[AuthenticatedSession, Depends(require(Permission.PLATFORM_TENANTS))]

#: 1 for an active user, 0 otherwise. A CASE rather than a cast so the
#: same expression runs on PostgreSQL and on the SQLite the tests use.
_ACTIVE = case((User.is_active.is_(True), 1), else_=0)


# ── Schemas ─────────────────────────────────────────────────────────────────


class TenantSummary(BaseModel):
    id: int
    slug: str
    display_name: str
    status: TenantStatus
    #: What is ENFORCED. A tenant mid-onboarding has none stored yet, which
    #: `onboarding_required` reports separately.
    plan: TenantPlan
    onboarding_required: bool
    locale: str
    users: int
    active_users: int
    orders: int
    created_at: datetime


class TenantPage(BaseModel):
    items: list[TenantSummary]
    total: int


class PlatformUser(BaseModel):
    id: int
    username: str
    email: str
    full_name: str
    position: str | None
    role: Role
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None


class PairPrice(BaseModel):
    source_language: str
    target_language: str
    price_per_page: Decimal
    is_active: bool


class TenantFigures(BaseModel):
    """The Reports tab, for one tenant, from outside it."""

    orders: int
    documents: int
    pages: int
    revenue: Decimal
    translator_cost: Decimal
    notary_cost: Decimal
    #: Revenue minus what the translators and notaries were paid. NOT the same
    #: figure as the Reports screen's profit, which also subtracts per-order
    #: expenses — those are a tenant's own bookkeeping and are deliberately
    #: not aggregated across tenants here.
    gross_profit: Decimal
    first_order: date | None
    last_order: date | None


class DriveLink(BaseModel):
    """Read-only here. A bureau connects its drive in Settings → Integrations,
    where the ownership check lives — the platform console only reports it,
    which is what support needs when someone says their files are missing."""

    #: None when the bureau has not linked one.
    shared_drive_id: str | None
    #: The name Google reported when it was linked — shown so a mistyped id
    #: reads as "that isn't their drive" rather than passing unnoticed.
    drive_name: str | None


class TenantDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tenant: TenantSummary
    users: list[PlatformUser]
    languages: list[str]
    pricing: list[PairPrice]
    figures: TenantFigures
    drive: DriveLink


class PlatformUserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    position: str | None = Field(default=None, max_length=100)
    role: Role = Role.ADMIN


class PlatformUserCreated(BaseModel):
    user: PlatformUser
    #: Shown once. There is no email step here on purpose — a platform
    #: operator creating an account for a bureau hands the credentials over
    #: directly, and mailing them to an address the operator typed would be
    #: sending a working password to whoever that turns out to be.
    one_time_password: str


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TenantStatus
    #: Written to the audit log. Suspending a paying customer should leave a
    #: reason behind, not just a timestamp.
    reason: str | None = Field(default=None, max_length=500)


class PlatformTotals(BaseModel):
    tenants: int
    active_tenants: int
    suspended_tenants: int
    by_plan: dict[str, int]
    users: int
    orders: int
    revenue: Decimal


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _tenant_or_404(db: Db, tenant_id: int) -> Tenant:
    with bypass_tenant_scope():
        tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFoundError("Tenant not found.")
    return tenant


def _summary(tenant: Tenant, *, users: int, active_users: int, orders: int) -> TenantSummary:
    from suliko.domain.plans import parse

    return TenantSummary(
        id=tenant.id,
        slug=tenant.slug,
        display_name=tenant.display_name,
        status=tenant.status,
        plan=effective_plan(tenant.plan),
        onboarding_required=parse(tenant.plan) is None,
        locale=tenant.locale,
        users=users,
        active_users=active_users,
        orders=orders,
        created_at=tenant.created_at,
    )


def _user_out(row: User) -> PlatformUser:
    return PlatformUser(
        id=row.id,
        username=row.username,
        email=row.email,
        full_name=row.full_name,
        position=row.position,
        role=row.role,
        is_active=row.is_active,
        must_change_password=row.must_change_password,
        last_login_at=row.last_login_at,
    )


# ── Tenants ─────────────────────────────────────────────────────────────────


@router.get("/tenants", response_model=TenantPage)
async def list_tenants(
    db: Db,
    _: Superuser,
    search: Annotated[str | None, Query(max_length=255)] = None,
    status: Annotated[TenantStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TenantPage:
    """Every bureau on the platform.

    The counts are grouped subqueries rather than a per-row lookup: fifty
    tenants should be three statements, not a hundred and fifty.
    """
    with bypass_tenant_scope():
        stmt = select(Tenant)
        if search:
            term = f"%{search.strip()}%"
            stmt = stmt.where(Tenant.slug.ilike(term) | Tenant.display_name.ilike(term))
        if status is not None:
            stmt = stmt.where(Tenant.status == status)

        total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        tenants = (
            (await db.execute(stmt.order_by(Tenant.id).limit(limit).offset(offset))).scalars().all()
        )

        ids = [t.id for t in tenants]
        users: dict[int, tuple[int, int]] = {}
        orders: dict[int, int] = {}

        if ids:
            for tenant_id, total_users, active in await db.execute(
                select(User.tenant_id, func.count(), func.coalesce(func.sum(_ACTIVE), 0))
                .where(User.tenant_id.in_(ids))
                .group_by(User.tenant_id)
            ):
                users[tenant_id] = (int(total_users), int(active or 0))

            for tenant_id, count in await db.execute(
                select(Order.tenant_id, func.count())
                .where(Order.tenant_id.in_(ids))
                .group_by(Order.tenant_id)
            ):
                orders[tenant_id] = int(count)

    return TenantPage(
        items=[
            _summary(
                t,
                users=users.get(t.id, (0, 0))[0],
                active_users=users.get(t.id, (0, 0))[1],
                orders=orders.get(t.id, 0),
            )
            for t in tenants
        ],
        total=total,
    )


@router.get("/tenants/{tenant_id}", response_model=TenantDetail)
async def get_tenant(tenant_id: int, db: Db, _: Superuser) -> TenantDetail:
    """One bureau: who works there, what they translate, what they charge,
    and what it adds up to."""
    tenant = await _tenant_or_404(db, tenant_id)

    with bypass_tenant_scope():
        users = (
            (await db.execute(select(User).where(User.tenant_id == tenant_id).order_by(User.id)))
            .scalars()
            .all()
        )

        languages = (
            (
                await db.execute(
                    select(Language.code)
                    .where(Language.tenant_id == tenant_id, Language.is_active.is_(True))
                    .order_by(Language.code)
                )
            )
            .scalars()
            .all()
        )

        pricing = (
            (
                await db.execute(
                    select(LanguagePairPrice)
                    .where(LanguagePairPrice.tenant_id == tenant_id)
                    .order_by(
                        LanguagePairPrice.source_language,
                        LanguagePairPrice.target_language,
                    )
                )
            )
            .scalars()
            .all()
        )

        figures = await _figures(db, tenant_id)

        drive_row = (
            await db.execute(select(DriveSettings).where(DriveSettings.tenant_id == tenant_id))
        ).scalar_one_or_none()

    return TenantDetail(
        tenant=_summary(
            tenant,
            users=len(users),
            active_users=sum(1 for u in users if u.is_active),
            orders=figures.orders,
        ),
        users=[_user_out(u) for u in users],
        languages=list(languages),
        pricing=[
            PairPrice(
                source_language=p.source_language,
                target_language=p.target_language,
                price_per_page=p.price_per_page,
                is_active=p.is_active,
            )
            for p in pricing
        ],
        figures=figures,
        drive=DriveLink(
            shared_drive_id=drive_row.shared_drive_id if drive_row else None,
            drive_name=drive_row.drive_name if drive_row else None,
        ),
    )


async def _figures(db: Db, tenant_id: int) -> TenantFigures:
    """Aggregates for one tenant, filtered explicitly rather than ambiently.

    Deliberately not built on `domain/orders.py`: those helpers assume a
    tenant is in context, and a cross-tenant caller must never depend on that.
    The predicate is right here in the statement instead.
    """
    row = (
        await db.execute(
            select(
                func.count(func.distinct(Order.id)),
                func.count(OrderDocument.id),
                func.coalesce(func.sum(OrderDocument.page_count), 0),
                func.coalesce(func.sum(OrderDocument.price), 0),
                func.coalesce(func.sum(OrderDocument.translator_cost), 0),
                func.coalesce(func.sum(OrderDocument.notary_cost), 0),
                func.min(Order.order_date),
                func.max(Order.order_date),
            )
            .select_from(Order)
            .outerjoin(OrderDocument, OrderDocument.order_id == Order.id)
            .where(Order.tenant_id == tenant_id)
        )
    ).one()

    revenue = Decimal(row[3] or 0)
    translator = Decimal(row[4] or 0)
    notary = Decimal(row[5] or 0)

    return TenantFigures(
        orders=int(row[0] or 0),
        documents=int(row[1] or 0),
        pages=int(row[2] or 0),
        revenue=revenue,
        translator_cost=translator,
        notary_cost=notary,
        gross_profit=revenue - translator - notary,
        first_order=row[6],
        last_order=row[7],
    )


@router.put("/tenants/{tenant_id}/status", response_model=TenantSummary)
async def set_tenant_status(
    tenant_id: int,
    payload: StatusChange,
    db: Db,
    session: Superuser,
) -> TenantSummary:
    """Suspend or reinstate a bureau.

    Suspension bites on the next request, not at session expiry —
    `resolve_session` checks `tenant.is_usable` on every call.
    """
    tenant = await _tenant_or_404(db, tenant_id)

    if tenant.id == session.tenant_id and payload.status is TenantStatus.SUSPENDED:
        # Otherwise the platform operator locks themselves out of the console
        # they would need to undo it.
        raise ConflictError("You cannot suspend the tenant you are signed in to.")

    before = tenant.status
    with bypass_tenant_scope():
        tenant.status = payload.status
        tenant.suspended_at = (
            datetime.now(UTC) if payload.status is TenantStatus.SUSPENDED else None
        )
        await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="platform.tenant_status_changed",
        entity_type="tenant",
        entity_id=tenant.id,
        tenant_id=tenant.id,
        before={"status": before.value},
        after={"status": payload.status.value, "reason": payload.reason},
    )

    figures = await _figures(db, tenant_id)
    with bypass_tenant_scope():
        counts = (
            await db.execute(
                select(func.count(), func.coalesce(func.sum(_ACTIVE), 0)).where(
                    User.tenant_id == tenant_id
                )
            )
        ).one()

    return _summary(
        tenant, users=int(counts[0]), active_users=int(counts[1] or 0), orders=figures.orders
    )


# ── Users, in somebody else's tenant ────────────────────────────────────────


@router.post(
    "/tenants/{tenant_id}/users",
    response_model=PlatformUserCreated,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_tenant_user(
    tenant_id: int,
    payload: PlatformUserCreate,
    db: Db,
    session: Superuser,
) -> PlatformUserCreated:
    """Create an account inside a tenant, from outside it.

    The reason this exists: a bureau whose only owner has left, or one being
    onboarded by hand. It is not a replacement for the tenant's own Users
    screen, and it deliberately cannot mint a superuser — that restriction is
    the same one `users.py` enforces and is asserted by
    `tests/test_user_management.py`.
    """
    if payload.role is Role.SUPERUSER:
        raise ValidationError(
            "Superuser cannot be assigned here. It is created from the server console."
        )

    tenant = await _tenant_or_404(db, tenant_id)

    email = str(payload.email).strip().lower()
    username = email if len(email) <= 100 else email.split("@")[0][:100]

    with bypass_tenant_scope():
        clash = (
            await db.execute(
                select(User).where(
                    User.tenant_id == tenant_id,
                    (User.username == username) | (User.email == email),
                )
            )
        ).scalar_one_or_none()
    if clash:
        raise ConflictError("That email already has an account in this organisation.")

    one_time_password = generate_one_time_password()

    # The GUC and the ORM stamp both point at the TARGET tenant, not the
    # operator's own. Binding them together is what stops a row landing in the
    # wrong bureau by omission.
    await bind_tenant_guc(db, tenant_id)
    with tenant_scope(tenant_id):
        row = User(
            tenant_id=tenant_id,
            username=username,
            email=email,
            full_name=payload.full_name.strip(),
            position=(payload.position or "").strip() or None,
            password_hash=await hash_password_async(one_time_password),
            role=payload.role,
            is_active=True,
            must_change_password=True,
        )
        db.add(row)
        await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="platform.user_created",
        entity_type="user",
        entity_id=row.id,
        tenant_id=tenant_id,
        after={
            "username": username,
            "role": payload.role.value,
            "tenant_slug": tenant.slug,
        },
    )

    return PlatformUserCreated(user=_user_out(row), one_time_password=one_time_password)


@router.delete("/tenants/{tenant_id}/users/{user_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_tenant_user(
    tenant_id: int,
    user_id: int,
    db: Db,
    session: Superuser,
) -> None:
    """Remove an account from a tenant.

    The tenant id is in the path and is checked against the row, so a mistyped
    user id cannot delete somebody in a different bureau.
    """
    with bypass_tenant_scope():
        row = await db.get(User, user_id)
    if row is None or row.tenant_id != tenant_id:
        raise NotFoundError("User not found in this organisation.")

    if row.id == session.user_id:
        raise ValidationError("You cannot delete your own account.")

    if row.role is Role.SUPERUSER:
        raise ConflictError(
            "A superuser cannot be deleted here. Remove it from the server console."
        )

    with bypass_tenant_scope():
        remaining_owners = (
            await db.scalar(
                select(func.count())
                .select_from(User)
                .where(
                    User.tenant_id == tenant_id,
                    User.role == Role.OWNER,
                    User.is_active.is_(True),
                    User.id != user_id,
                )
            )
        ) or 0
    if row.role is Role.OWNER and remaining_owners == 0:
        raise ConflictError(
            "This is the tenant's last active owner. Create a replacement first, "
            "or the bureau is left with nobody who can administer it."
        )

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="platform.user_deleted",
        entity_type="user",
        entity_id=row.id,
        tenant_id=tenant_id,
        before={"username": row.username, "role": row.role.value},
    )

    await revoke_all_for_user(db, row.id)
    with bypass_tenant_scope():
        await db.delete(row)


# ── The platform, in total ──────────────────────────────────────────────────


@router.get("/stats", response_model=PlatformTotals)
async def platform_stats(db: Db, _: Superuser) -> PlatformTotals:
    """Everything, summed. The Reports tab one level up."""
    with bypass_tenant_scope():
        by_status: dict[TenantStatus, int] = {
            status: int(count)
            for status, count in (
                await db.execute(select(Tenant.status, func.count()).group_by(Tenant.status))
            ).all()
        }

        # Grouped by the STORED value, then folded onto the enforced one —
        # a tenant mid-onboarding has null stored and counts as the default.
        plans: dict[str, int] = {}
        for stored, count in (
            await db.execute(select(Tenant.plan, func.count()).group_by(Tenant.plan))
        ).all():
            key = effective_plan(stored).value
            plans[key] = plans.get(key, 0) + int(count)

        users = await db.scalar(select(func.count()).select_from(User)) or 0
        orders = await db.scalar(select(func.count()).select_from(Order)) or 0
        revenue = (await db.scalar(select(func.coalesce(func.sum(OrderDocument.price), 0)))) or 0

    return PlatformTotals(
        tenants=sum(by_status.values()),
        active_tenants=int(by_status.get(TenantStatus.ACTIVE, 0))
        + int(by_status.get(TenantStatus.TRIAL, 0)),
        suspended_tenants=int(by_status.get(TenantStatus.SUSPENDED, 0)),
        by_plan=plans,
        users=int(users),
        orders=int(orders),
        revenue=Decimal(revenue),
    )


__all__ = ["router"]
