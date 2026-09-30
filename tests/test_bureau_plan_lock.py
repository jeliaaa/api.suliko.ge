"""A bureau stays a bureau: owners cannot move an organisation on the Bureau
plan to Freelancer (decided 2026-09-30). Freelance work belongs in the
personal account, which is exempt."""

from __future__ import annotations

import inspect

import pytest

from suliko.api.v1 import tenant as tenant_api
from suliko.api.v1.tenant import bureau_stays_bureau
from suliko.domain.plans import TenantPlan
from suliko.models.tenant import Tenant


def _tenant(plan: str | None, *, personal: bool = False) -> Tenant:
    return Tenant(slug="acme", display_name="Acme", plan=plan, is_personal=personal)


def test_a_bureau_cannot_become_a_freelancer() -> None:
    assert bureau_stays_bureau(_tenant("bureau"), TenantPlan.FREELANCER)


def test_a_bureau_can_stay_a_bureau() -> None:
    assert not bureau_stays_bureau(_tenant("bureau"), TenantPlan.BUREAU)


@pytest.mark.parametrize("plan", [None, "freelancer"])
def test_an_organisation_not_yet_a_bureau_may_choose_either(plan: str | None) -> None:
    """Onboarding after sign-up (plan unset), and a freelancer-plan
    organisation: both choices stay open, including upgrading."""
    for wanted in TenantPlan:
        assert not bureau_stays_bureau(_tenant(plan), wanted)


def test_the_personal_account_may_move_either_way() -> None:
    assert not bureau_stays_bureau(_tenant("bureau", personal=True), TenantPlan.FREELANCER)


def test_the_plan_endpoint_applies_the_rule_before_saving() -> None:
    source = inspect.getsource(tenant_api.choose_plan)
    assert source.index("bureau_stays_bureau(") < source.index("current.plan = ")


def test_a_bureau_created_after_sign_in_starts_on_the_bureau_plan() -> None:
    """No plan choice for it: it is a bureau from the first request."""
    from suliko.domain import accounts

    default = inspect.signature(accounts.create_bureau).parameters["plan"].default
    assert default is TenantPlan.BUREAU
