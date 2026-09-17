"""What a tenant's subscription plan lets it do.

Mirrors ``app.suliko.ge/src/shared/auth/plans.ts``; ``tests/test_parity.py``
asserts the two agree. As with permissions, the TypeScript copy hides tabs and
this copy is the one that enforces.

## Two axes, not one

A **role** says what a person may do inside their bureau. A **plan** says what
the bureau bought. They are independent, and a request needs both to say yes:

    effective = ROLE_PERMISSIONS[role] & PLAN_PERMISSIONS[plan]

Expressing the plan as a permission mask rather than as a separate check is
what makes this cheap to get right. Every handler in the codebase already
guards on a permission string, and the sidebar already hides what the session
lacks — so the intersection reaches all of them at once, with no new call
sites to forget. A freelancer's owner role keeps every permission the role
grants; the plan is what withholds `users.manage`.

## Freelancer

One person, no employees, no payroll. The tabs are Dashboard, Translations,
Clients, Notaries, Calculator, Reports and Settings; the only external service
is Google Drive. They are still the OWNER of their tenant — they can change
their own settings and their own billing — they simply have nobody to manage
and nobody to pay.

Note what this deliberately withholds: `finance.*`. A freelancer invoices
through the Translations screen and the invoice document; the Finances screen
is about paying translators and reconciling a bureau's books, which is not a
thing a sole trader does here. If that turns out to be wrong it is one line.

## A null plan

``tenants.plan`` is nullable, and null means "signed up, has not chosen yet".
Such a tenant is treated as a freelancer for access and is sent to onboarding
until it picks. Every tenant that existed before plans were introduced was
backfilled to ``bureau`` by revision 0005 — otherwise switching this on would
have silently taken the Finances and Users tabs away from working bureaus.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType

from suliko.domain.pricing import PricingConfig
from suliko.models.integration import IntegrationProvider
from suliko.models.user import Role
from suliko.security.permissions import Permission, permissions_for_role


class TenantPlan(enum.StrEnum):
    FREELANCER = "freelancer"
    BUREAU = "bureau"


#: What a brand-new self-signup is treated as until it chooses. The narrower of
#: the two on purpose: showing someone a tab their plan does not include and
#: taking it away a moment later is worse than revealing it when they upgrade.
DEFAULT_PLAN = TenantPlan.FREELANCER

P = Permission

_FREELANCER: frozenset[Permission] = frozenset(
    {
        P.ORDERS_READ,
        P.ORDERS_WRITE,
        P.ORDERS_DELETE,
        P.ORDERS_CHANGE_STATUS,
        P.CLIENTS_READ,
        P.CLIENTS_WRITE,
        P.NOTARIES_READ,
        P.NOTARIES_WRITE,
        P.REPORTS_READ,
        P.REPORTS_PROFIT,
        P.SETTINGS_MANAGE,
        # They own the tenant even though they are its only member.
        P.TENANT_MANAGE,
        P.TENANT_BILLING,
    }
)

#: Everything. A bureau's ROLE decides; the plan withholds nothing.
_BUREAU: frozenset[Permission] = frozenset(Permission)

PLAN_PERMISSIONS: MappingProxyType[TenantPlan, frozenset[Permission]] = MappingProxyType(
    {
        TenantPlan.FREELANCER: _FREELANCER,
        TenantPlan.BUREAU: _BUREAU,
    }
)


class Feature(enum.StrEnum):
    """Screens that are not gated by any permission.

    Dashboard, Calculator and Notifications have no permission of their own —
    every role may use them — so the permission mask cannot reach them and
    they need naming explicitly.
    """

    NOTIFICATIONS = "notifications"


#: Notifications are internal notes and @mentions between colleagues. A sole
#: trader has no colleagues, so the whole screen is noise rather than a feature
#: being withheld.
PLAN_FEATURES: MappingProxyType[TenantPlan, frozenset[Feature]] = MappingProxyType(
    {
        TenantPlan.FREELANCER: frozenset(),
        TenantPlan.BUREAU: frozenset(Feature),
    }
)

#: External services each plan may connect. Invoicing is not here because it is
#: not an integration — it is built in, and both plans have it.
PLAN_PROVIDERS: MappingProxyType[TenantPlan, frozenset[IntegrationProvider]] = MappingProxyType(
    {
        TenantPlan.FREELANCER: frozenset({IntegrationProvider.GOOGLE_DRIVE}),
        TenantPlan.BUREAU: frozenset(IntegrationProvider),
    }
)


def parse(value: str | None) -> TenantPlan | None:
    """A stored plan string, or None when unset or unrecognised.

    Unrecognised is folded into None rather than raising: a row carrying a
    plan this build does not know about (a downgrade, a hand-edited database)
    should send someone to onboarding, not 500 their every request.
    """
    if not value:
        return None
    try:
        return TenantPlan(value)
    except ValueError:
        return None


def effective_plan(value: str | None) -> TenantPlan:
    """The plan to enforce, treating "not chosen yet" as the default."""
    return parse(value) or DEFAULT_PLAN


def permissions_for_plan(plan: TenantPlan) -> frozenset[Permission]:
    return PLAN_PERMISSIONS.get(plan, _FREELANCER)


#: Permissions an owner may NOT hand to an employee, whatever the plan says.
#:
#: Platform permissions belong to the superuser role and reach across tenants.
#: Nothing a bureau owner can click may grant them — that would turn "edit an
#: employee" into a path out of the tenant. Enforced on the write side too;
#: this is the one that matters, because it is what a stale or hand-edited row
#: runs into.
NON_OVERRIDABLE: frozenset[Permission] = frozenset(
    {
        Permission.PLATFORM_TENANTS,
        Permission.PLATFORM_IMPERSONATE,
        Permission.PLATFORM_AUDIT,
    }
)

#: What an owner's checkbox list may cover.
OVERRIDABLE: frozenset[Permission] = frozenset(Permission) - NON_OVERRIDABLE


def effective_permissions(
    role: Role,
    plan: TenantPlan,
    overrides: Mapping[str, bool] | None = None,
) -> frozenset[Permission]:
    """What this person may actually do.

    Three inputs, applied in an order that is load-bearing:

    1. the ROLE's bundle — the starting point and the sane default;
    2. the per-user OVERRIDES — what the owner ticked or unticked for this
       person specifically, stored as a difference against the role;
    3. the PLAN, intersected LAST.

    The plan going last is what makes overrides safe. An owner can tick any
    box the form offers, and the result still cannot exceed what the bureau
    bought — so a freelancer who somehow acquires a `users.manage` override
    (a downgrade after inviting staff, say) does not get it back.

    A permission string this build does not recognise is ignored rather than
    raising: the catalogue changes, rows outlive it, and one stale row must
    not 500 every request the user makes.
    """
    granted = set(permissions_for_role(role))

    for name, allow in (overrides or {}).items():
        try:
            permission = Permission(name)
        except ValueError:
            continue
        if permission in NON_OVERRIDABLE:
            # Never grantable, and never revocable either — a superuser's
            # platform access is not a tenant's to edit in either direction.
            continue
        if allow:
            granted.add(permission)
        else:
            granted.discard(permission)

    return frozenset(granted) & permissions_for_plan(plan)


def overridable_for_plan(plan: TenantPlan) -> frozenset[Permission]:
    """The boxes an owner's form should offer on this plan.

    Offering one the plan withholds would let someone tick it, save, and see
    nothing change — the intersection above would drop it again on the next
    request, with no explanation anywhere.
    """
    return OVERRIDABLE & permissions_for_plan(plan)


def allows_feature(plan: TenantPlan, feature: Feature) -> bool:
    return feature in PLAN_FEATURES.get(plan, frozenset())


def allows_provider(plan: TenantPlan, provider: IntegrationProvider) -> bool:
    return provider in PLAN_PROVIDERS.get(plan, frozenset())


def providers_for_plan(plan: TenantPlan) -> frozenset[IntegrationProvider]:
    return PLAN_PROVIDERS.get(plan, frozenset())


def pricing_for_plan(config: PricingConfig, plan: TenantPlan) -> PricingConfig:
    """Adjust a tenant's pricing knobs for what its plan actually is.

    A freelancer does the translation themselves. The plan withholds
    `translators.*`, so there is nobody to assign and nobody to pay — and the
    bureau default of a 50% translator share would book half of every job as a
    cost paid to no one, halving the profit their Reports screen shows.

    Applied at quote time AND at order time, so the price a freelancer is shown
    is the cost the order is stored with.
    """
    if plan is TenantPlan.FREELANCER:
        return replace(config, translator_share=Decimal("0"))
    return config


__all__ = [
    "DEFAULT_PLAN",
    "NON_OVERRIDABLE",
    "OVERRIDABLE",
    "PLAN_FEATURES",
    "PLAN_PERMISSIONS",
    "PLAN_PROVIDERS",
    "Feature",
    "TenantPlan",
    "allows_feature",
    "allows_provider",
    "effective_permissions",
    "effective_plan",
    "overridable_for_plan",
    "parse",
    "permissions_for_plan",
    "pricing_for_plan",
    "providers_for_plan",
]
