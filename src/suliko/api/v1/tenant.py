"""The caller's own tenant: what it is, and which plan it is on.

Distinct from the platform router (still deferred), which is about OTHER
tenants and is superuser-only. Everything here acts on the tenant already
bound to the session, and the tenant id is never taken from the request.

## Choosing a plan

`tenants.plan` is null from sign-up until onboarding sets it. That null is the
whole onboarding state machine: the session reports `onboarding_required`
while it holds, and the frontend sends the owner to the onboarding screen
until they choose. There is no separate "has seen the tour" flag, because a
plan that has been chosen is exactly the thing onboarding exists to produce.

Owners can change very little here now (2026-09-30). An organisation on the
Bureau plan stays a bureau, and the personal account (`tenants.is_personal`)
stays Freelancer: a freelancer who wants a team creates a bureau from the
account switcher rather than converting their own workspace. What is left is
the onboarding choice after sign-up, and a Freelancer-plan organisation moving
up to Bureau. The platform admin can still change any tenant's plan.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from suliko.api.deps import CurrentSession, Db, require
from suliko.core.errors import ConflictError, NotFoundError
from suliko.domain.plans import (
    PLAN_FEATURES,
    PLAN_PROVIDERS,
    Feature,
    TenantPlan,
    parse,
    permissions_for_plan,
)
from suliko.models.tenant import Tenant, TenantStatus
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

router = APIRouter(prefix="/tenant", tags=["tenant"])


class PlanOut(BaseModel):
    plan: TenantPlan
    #: Null until onboarding. Callers that need to know "has chosen" read this
    #: rather than comparing `plan` against the default, which is ambiguous.
    chosen: bool
    permissions: list[str]
    features: list[Feature]
    providers: list[str]


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    display_name: str
    status: TenantStatus
    locale: str
    onboarding_required: bool
    plans: dict[str, PlanOut]
    current: PlanOut


class BureauPlanLockedError(ConflictError):
    error_code = "bureau_plan_locked"


class PersonalPlanLockedError(ConflictError):
    error_code = "personal_plan_locked"


def bureau_stays_bureau(tenant: Tenant, wanted: TenantPlan) -> bool:
    """True when this change is refused: an organisation on the Bureau plan
    moving to Freelancer. Freelance work belongs in the personal account."""
    return (
        not tenant.is_personal
        and parse(tenant.plan) is TenantPlan.BUREAU
        and wanted is TenantPlan.FREELANCER
    )


def personal_stays_freelancer(tenant: Tenant, wanted: TenantPlan) -> bool:
    """True when this change is refused: the personal account leaving the
    Freelancer plan. A team gets its own bureau, from the account switcher."""
    return tenant.is_personal and wanted is not TenantPlan.FREELANCER


class PlanChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: TenantPlan


def _plan_out(plan: TenantPlan, *, chosen: bool) -> PlanOut:
    return PlanOut(
        plan=plan,
        chosen=chosen,
        permissions=sorted(p.value for p in permissions_for_plan(plan)),
        features=sorted(PLAN_FEATURES[plan]),
        providers=sorted(p.value for p in PLAN_PROVIDERS[plan]),
    )


@router.get("", response_model=TenantOut)
async def get_tenant(db: Db, session: CurrentSession) -> TenantOut:
    """The caller's own bureau, plus what each plan would give them.

    Both plans are returned, not just the current one, because the onboarding
    and upgrade screens have to show the comparison — and deriving it in the
    frontend would be a second copy of the plan tables to keep in step.
    """
    tenant = await db.get(Tenant, session.tenant_id)
    if tenant is None:
        raise NotFoundError("Tenant not found.")

    stored = parse(tenant.plan)

    return TenantOut(
        id=tenant.id,
        slug=tenant.slug,
        display_name=tenant.display_name,
        status=tenant.status,
        locale=tenant.locale,
        onboarding_required=stored is None,
        plans={plan.value: _plan_out(plan, chosen=plan == stored) for plan in TenantPlan},
        current=_plan_out(session.plan, chosen=stored is not None),
    )


@router.put("/plan", response_model=TenantOut)
async def choose_plan(
    payload: PlanChoice,
    db: Db,
    session: Annotated[AuthenticatedSession, Depends(require(Permission.TENANT_MANAGE))],
) -> TenantOut:
    """Set the plan. This is what completes onboarding.

    Gated on `tenant.manage`, which only the owner has — and which the
    freelancer plan deliberately keeps, so a freelancer can upgrade
    themselves without going through support.
    """
    current = await db.get(Tenant, session.tenant_id)
    if current is None:
        raise NotFoundError("Tenant not found.")

    before = current.plan
    if personal_stays_freelancer(current, payload.plan):
        raise PersonalPlanLockedError(
            "Your personal account stays on the Freelancer plan. "
            "To work with a team, create a bureau from the account switcher."
        )
    if bureau_stays_bureau(current, payload.plan):
        raise BureauPlanLockedError(
            "A bureau cannot switch to the Freelancer plan. "
            "Use your personal account for freelance work."
        )
    current.plan = payload.plan.value
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="tenant.plan_chosen" if before is None else "tenant.plan_changed",
        entity_type="tenant",
        entity_id=current.id,
        before={"plan": before},
        after={"plan": current.plan},
    )

    # The session in hand still carries the OLD permission mask — it was built
    # when the request came in. The frontend re-reads /auth/session after this
    # call, which is where the new mask arrives.
    return await get_tenant(db=db, session=session)


__all__ = ["router"]
