"""Per-user permission overrides.

Overrides are the second way (after roles) that access is decided, and the
first one an owner can edit from a form. So the question these answer is not
"does the arithmetic work" but "can a form reach somewhere a form should not":
out past the plan, out past the granter's own access, or out of the tenant
altogether.

The handler guards are checked by reading their source, the way
`test_user_management.py` does — `users.py` needs a database and these must
run without one.
"""

from __future__ import annotations

import inspect

import pytest

from suliko.domain.plans import (
    NON_OVERRIDABLE,
    OVERRIDABLE,
    TenantPlan,
    effective_permissions,
    overridable_for_plan,
)
from suliko.models.user import Role
from suliko.security.permissions import Permission, permissions_for_role

P = Permission


# ── The arithmetic ──────────────────────────────────────────────────────────


def test_a_grant_adds_what_the_role_withholds() -> None:
    """The ordinary case: a staff member trusted with the books."""
    assert P.FINANCE_READ not in permissions_for_role(Role.STAFF)

    effective = effective_permissions(Role.STAFF, TenantPlan.BUREAU, {"finance.read": True})
    assert P.FINANCE_READ in effective


def test_a_revocation_removes_what_the_role_grants() -> None:
    """The other direction, and the one a checkbox list makes easy to hit: a
    manager who should not be deleting orders."""
    assert P.ORDERS_DELETE in permissions_for_role(Role.MANAGER)

    effective = effective_permissions(Role.MANAGER, TenantPlan.BUREAU, {"orders.delete": False})
    assert P.ORDERS_DELETE not in effective


def test_no_overrides_leaves_the_role_alone() -> None:
    for role in Role:
        assert effective_permissions(role, TenantPlan.BUREAU, {}) == effective_permissions(
            role, TenantPlan.BUREAU
        )


def test_an_unknown_permission_is_ignored() -> None:
    """A row naming a permission this build has dropped must not 500 every
    request the user makes — the catalogue changes and rows outlive it."""
    effective = effective_permissions(
        Role.STAFF, TenantPlan.BUREAU, {"orders.timetravel": True, "finance.read": True}
    )
    assert P.FINANCE_READ in effective


# ── The plan still wins ─────────────────────────────────────────────────────


def test_an_override_cannot_exceed_the_plan() -> None:
    """The ordering that makes overrides safe: the plan is intersected LAST.

    This is the real scenario — a bureau invites staff, then downgrades to
    freelancer. The override rows survive, and must stop meaning anything.
    """
    effective = effective_permissions(
        Role.STAFF, TenantPlan.FREELANCER, {"users.manage": True, "finance.read": True}
    )
    assert P.USERS_MANAGE not in effective
    assert P.FINANCE_READ not in effective


@pytest.mark.parametrize("plan", list(TenantPlan))
def test_the_offered_boxes_never_exceed_the_plan(plan: TenantPlan) -> None:
    """Offering a box the plan withholds lets someone tick it, save, and see
    nothing change — with no explanation anywhere."""
    assert overridable_for_plan(plan) <= set(
        effective_permissions(Role.OWNER, plan) | overridable_for_plan(plan)
    )
    for permission in overridable_for_plan(plan):
        effective = effective_permissions(Role.STAFF, plan, {permission.value: True})
        assert permission in effective, f"{permission.value} is offered but cannot be granted"


# ── Platform access is never a tenant's to give ─────────────────────────────


@pytest.mark.parametrize(
    "permission", [P.PLATFORM_TENANTS, P.PLATFORM_IMPERSONATE, P.PLATFORM_AUDIT]
)
def test_a_platform_permission_cannot_be_granted_by_an_override(
    permission: Permission,
) -> None:
    """The one that would be an escape from the tenant, not merely a
    permission too many. Guarded on write too; this is the read-side backstop
    that catches a row written before the guard existed, or by hand."""
    effective = effective_permissions(Role.OWNER, TenantPlan.BUREAU, {permission.value: True})
    assert permission not in effective


def test_a_platform_permission_cannot_be_revoked_either() -> None:
    """A superuser's platform access is not a tenant's to edit in either
    direction — a bureau owner must not be able to disable the platform
    administrator who is investigating them."""
    effective = effective_permissions(Role.SUPERUSER, TenantPlan.BUREAU, {"platform.audit": False})
    assert P.PLATFORM_AUDIT in effective


def test_the_offered_list_excludes_platform_permissions() -> None:
    assert not (OVERRIDABLE & NON_OVERRIDABLE)
    assert set(NON_OVERRIDABLE) == {
        P.PLATFORM_TENANTS,
        P.PLATFORM_IMPERSONATE,
        P.PLATFORM_AUDIT,
    }


# ── The handler guards ──────────────────────────────────────────────────────


def _source(name: str) -> str:
    from suliko.api.v1 import users

    return inspect.getsource(getattr(users, name))


def test_nobody_grants_access_they_do_not_have() -> None:
    """Without this, `users.manage` is a full escalation: an admin who cannot
    make bank transfers grants `finance.transfer` to an account they control,
    and then uses it."""
    source = _source("_guard_grantable")
    assert "session.permissions" in source, (
        "_guard_grantable no longer compares against the actor's own access"
    )

    for handler in ("invite_user", "update_user"):
        assert "_guard_grantable" in _source(handler), (
            f"{handler} does not check that the actor holds what it is handing out"
        )


def test_you_cannot_edit_your_own_access() -> None:
    """The twin of the existing "cannot change your own role" rule. Both have
    to hold, or `users.manage` grants itself everything else."""
    source = _source("update_user")
    assert "row.id == session.user_id" in source
    assert "cannot change your own access" in source


def test_an_access_change_revokes_their_sessions() -> None:
    """A session carries the permission set built when it was resolved, so a
    revocation that does not revoke sessions leaves the old access standing
    for up to the idle timeout."""
    assert "revoke_all_for_user" in _source("update_user")
    assert "permissions is not None" in _source("update_user")


def test_an_invite_never_logs_the_password() -> None:
    """`redact` would strip it, but the reliable way to keep a secret out of
    a log is not to hand it over."""
    source = _source("invite_user")
    after = source.split("await record(", 1)[1].split(")", 1)[0]
    assert "one_time_password" not in after


def test_an_invited_account_must_change_its_password() -> None:
    """Otherwise the inviter keeps a working credential for someone else's
    account indefinitely."""
    assert "must_change_password=True" in _source("invite_user")


def test_an_admin_reset_also_forces_a_change() -> None:
    assert "must_change_password = True" in _source("reset_password")
