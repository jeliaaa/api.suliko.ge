"""What each plan withholds.

The plan is a second access boundary next to the role, and it is enforced by
intersection — so the failure mode is not "a check was forgotten" but "a set
was written wrong", and a set written wrong is silent. These pin the shape of
both sets and the arithmetic between them.
"""

from __future__ import annotations

import pytest

from suliko.domain.plans import (
    DEFAULT_PLAN,
    PLAN_FEATURES,
    PLAN_PERMISSIONS,
    PLAN_PROVIDERS,
    Feature,
    TenantPlan,
    allows_feature,
    allows_provider,
    effective_permissions,
    effective_plan,
    parse,
    permissions_for_plan,
)
from suliko.models.integration import IntegrationProvider
from suliko.models.user import Role
from suliko.security.permissions import Permission, permissions_for_role

P = Permission


# ── Every plan is decided ───────────────────────────────────────────────────


@pytest.mark.parametrize("plan", list(TenantPlan))
def test_every_plan_has_a_complete_entry(plan: TenantPlan) -> None:
    """A plan missing from one of the three tables would fall through to a
    default, and the default is silent."""
    assert plan in PLAN_PERMISSIONS
    assert plan in PLAN_FEATURES
    assert plan in PLAN_PROVIDERS


def test_a_bureau_is_limited_only_by_role() -> None:
    """The plan must withhold nothing from a bureau, or a role that grants a
    permission would still be refused and the cause would be invisible."""
    assert permissions_for_plan(TenantPlan.BUREAU) == frozenset(Permission)


def test_freelancer_is_a_strict_subset_of_bureau() -> None:
    assert permissions_for_plan(TenantPlan.FREELANCER) < permissions_for_plan(TenantPlan.BUREAU)


# ── What the freelancer plan withholds ──────────────────────────────────────


@pytest.mark.parametrize(
    "permission",
    [
        # No employees: nobody to invite, nobody to manage.
        P.USERS_MANAGE,
        # No payroll and no bureau books.
        P.FINANCE_READ,
        P.FINANCE_RECORD_PAYMENT,
        P.FINANCE_REFUND,
        P.FINANCE_TRANSFER,
        P.FINANCE_EXPENSES,
        # A freelancer IS the translator; there is no roster to keep.
        P.TRANSLATORS_READ,
        P.TRANSLATORS_WRITE,
        # The marketing-site CMS and API keys belong to the bureau product.
        P.CMS_MANAGE,
        P.APIKEYS_MANAGE,
    ],
)
def test_the_freelancer_plan_withholds(permission: Permission) -> None:
    assert permission not in permissions_for_plan(TenantPlan.FREELANCER)


@pytest.mark.parametrize(
    "permission",
    [
        P.ORDERS_READ,
        P.ORDERS_WRITE,
        P.CLIENTS_READ,
        P.CLIENTS_WRITE,
        P.NOTARIES_READ,
        P.NOTARIES_WRITE,
        P.REPORTS_READ,
        P.REPORTS_PROFIT,
        P.SETTINGS_MANAGE,
        # They own their own tenant, so they can upgrade without support.
        P.TENANT_MANAGE,
        P.TENANT_BILLING,
    ],
)
def test_the_freelancer_plan_grants(permission: Permission) -> None:
    """The tabs from the spec: Dashboard, Translations, Clients, Notaries,
    Calculator, Reports, Settings."""
    assert permission in permissions_for_plan(TenantPlan.FREELANCER)


def test_no_plan_grants_a_platform_permission() -> None:
    """Platform permissions belong to the superuser role, not to anything a
    tenant can buy."""
    for plan in TenantPlan:
        granted = permissions_for_plan(plan)
        if plan is TenantPlan.BUREAU:
            continue  # bureau is "everything the ROLE allows" by construction
        assert not (granted & {P.PLATFORM_TENANTS, P.PLATFORM_IMPERSONATE, P.PLATFORM_AUDIT})


# ── Role and plan must BOTH allow ───────────────────────────────────────────


def test_a_freelancer_owner_loses_what_the_plan_withholds() -> None:
    """The case the whole design turns on: the role says yes, the plan says
    no, and the answer is no."""
    owner = permissions_for_role(Role.OWNER)
    assert P.USERS_MANAGE in owner

    effective = effective_permissions(Role.OWNER, TenantPlan.FREELANCER)
    assert P.USERS_MANAGE not in effective
    assert P.ORDERS_WRITE in effective


def test_a_bureau_staff_member_still_loses_what_the_role_withholds() -> None:
    """The mask must not become a grant. A generous plan cannot widen a role."""
    effective = effective_permissions(Role.STAFF, TenantPlan.BUREAU)
    assert effective == permissions_for_role(Role.STAFF)
    assert P.USERS_MANAGE not in effective
    assert P.FINANCE_TRANSFER not in effective


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("plan", list(TenantPlan))
def test_effective_never_exceeds_either_input(role: Role, plan: TenantPlan) -> None:
    effective = effective_permissions(role, plan)
    assert effective <= permissions_for_role(role)
    assert effective <= permissions_for_plan(plan)


# ── Features and providers ──────────────────────────────────────────────────


def test_notifications_are_a_bureau_feature() -> None:
    """Internal notes between colleagues. A sole trader has none."""
    assert allows_feature(TenantPlan.BUREAU, Feature.NOTIFICATIONS)
    assert not allows_feature(TenantPlan.FREELANCER, Feature.NOTIFICATIONS)


def test_a_freelancer_gets_drive_and_nothing_else() -> None:
    """The spec's "only Google Drive and Invoices". Invoicing is built in
    rather than an integration, so Drive is the whole list here."""
    assert PLAN_PROVIDERS[TenantPlan.FREELANCER] == frozenset({IntegrationProvider.GOOGLE_DRIVE})


@pytest.mark.parametrize("provider", list(IntegrationProvider))
def test_a_bureau_may_connect_every_provider(provider: IntegrationProvider) -> None:
    assert allows_provider(TenantPlan.BUREAU, provider)


def test_a_freelancer_may_not_connect_a_bank() -> None:
    """Named explicitly because it is the one with money attached."""
    assert not allows_provider(TenantPlan.FREELANCER, IntegrationProvider.BOG_BUSINESS)


# ── Reading the stored value ────────────────────────────────────────────────


def test_a_null_plan_means_not_chosen() -> None:
    assert parse(None) is None
    assert parse("") is None


def test_an_unknown_plan_is_not_an_error() -> None:
    """A row carrying a plan this build does not know — a rollback, a
    hand-edited database — must send someone to onboarding, not 500 every
    request they make."""
    assert parse("enterprise-plus") is None


def test_an_unchosen_plan_is_enforced_as_the_default() -> None:
    assert effective_plan(None) is DEFAULT_PLAN
    assert effective_plan("enterprise-plus") is DEFAULT_PLAN


def test_the_default_is_the_narrower_plan() -> None:
    """Revealing a tab on upgrade is fine; taking one away a moment after
    showing it is not."""
    assert DEFAULT_PLAN is TenantPlan.FREELANCER
    assert permissions_for_plan(DEFAULT_PLAN) < permissions_for_plan(TenantPlan.BUREAU)


@pytest.mark.parametrize("plan", list(TenantPlan))
def test_a_stored_plan_round_trips(plan: TenantPlan) -> None:
    """`tenants.plan` is a plain String column, so the enum value is the
    storage format and renaming one is a migration."""
    assert parse(plan.value) is plan
