"""`users.invite`: what a Manager may do about people, and what they may not.

Decided 2026-10-15: a Manager may invite up to Manager. The permission is the
smaller half of `users.manage`: invite, send an invitation again, and see the
members at or below your own rank, read-only. Everything that changes a person
stays behind `users.manage`. The model tests are here; the handlers that run
against a database are in `test_portal_api.py` next to the other invitation
tests, which already have the tenants and sessions for them.
"""

from __future__ import annotations

import inspect

import pytest

from suliko.api.v1 import users
from suliko.domain.plans import TenantPlan, effective_permissions, permissions_for_plan
from suliko.models.user import Role
from suliko.security.permissions import (
    Permission,
    permissions_for_role,
    requires_step_up,
)

P = Permission


@pytest.mark.parametrize(
    ("role", "invites"),
    [
        (Role.STAFF, False),
        (Role.MANAGER, True),
        (Role.ADMIN, True),
        (Role.OWNER, True),
        (Role.SUPERUSER, True),
    ],
)
def test_who_holds_the_invite_permission(role: Role, invites: bool) -> None:
    assert (P.USERS_INVITE in permissions_for_role(role)) is invites


def test_a_manager_does_not_hold_users_manage() -> None:
    """The point of the split: inviting without managing."""
    manager = permissions_for_role(Role.MANAGER)
    assert P.USERS_INVITE in manager
    assert P.USERS_MANAGE not in manager


def test_a_freelancer_workspace_cannot_invite() -> None:
    """A sole trader has no one to invite; the plan withholds it, as it does
    `users.manage`, however the role or an override reads."""
    assert P.USERS_INVITE not in permissions_for_plan(TenantPlan.FREELANCER)
    assert P.USERS_INVITE not in effective_permissions(
        Role.OWNER, TenantPlan.FREELANCER, {"users.invite": True}
    )


def test_a_bureau_manager_can_invite_and_staff_cannot() -> None:
    assert P.USERS_INVITE in effective_permissions(Role.MANAGER, TenantPlan.BUREAU)
    assert P.USERS_INVITE not in effective_permissions(Role.STAFF, TenantPlan.BUREAU)


def test_an_owner_can_give_staff_the_right_to_invite() -> None:
    granted = effective_permissions(Role.STAFF, TenantPlan.BUREAU, {"users.invite": True})
    assert P.USERS_INVITE in granted
    assert P.USERS_MANAGE not in granted


def test_an_owner_can_take_it_from_a_manager() -> None:
    manager = effective_permissions(Role.MANAGER, TenantPlan.BUREAU, {"users.invite": False})
    assert P.USERS_INVITE not in manager


def test_managing_people_includes_inviting_them() -> None:
    """An admin an owner un-ticked `users.invite` for keeps the Users screen,
    because they keep `users.manage`."""
    admin = effective_permissions(Role.ADMIN, TenantPlan.BUREAU, {"users.invite": False})
    assert P.USERS_MANAGE in admin
    assert P.USERS_INVITE in admin
    # And ticking `users.manage` on staff brings it along.
    staff = effective_permissions(Role.STAFF, TenantPlan.BUREAU, {"users.manage": True})
    assert P.USERS_INVITE in staff


def test_inviting_keeps_asking_for_a_fresh_code_where_managing_did() -> None:
    """Inviting used to sit behind `users.manage`, and so behind step-up for
    anyone with a second factor. Moving it must not lower that bar."""
    assert requires_step_up(P.USERS_INVITE)
    assert requires_step_up(P.USERS_MANAGE)


@pytest.mark.parametrize("handler", ["list_users", "get_user", "lookup_account", "invite_user"])
def test_the_screens_a_manager_uses_ask_for_users_invite(handler: str) -> None:
    source = inspect.getsource(getattr(users, handler))
    assert "require(Permission.USERS_INVITE)" in source
    assert "require(Permission.USERS_MANAGE)" not in source


def test_sending_an_invitation_again_asks_for_users_invite() -> None:
    source = inspect.getsource(users.resend_invitation)
    assert "require(Permission.USERS_INVITE)" in source
    assert "_guard_resend(db, session, row)" in source


@pytest.mark.parametrize("handler", ["update_user", "reset_user_mfa", "delete_user"])
def test_changing_a_person_still_asks_for_users_manage(handler: str) -> None:
    source = inspect.getsource(getattr(users, handler))
    assert "require(Permission.USERS_MANAGE)" in source
    assert "require(Permission.USERS_INVITE)" not in source
