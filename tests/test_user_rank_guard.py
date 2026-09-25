"""Nobody with `users.manage` may act on an account at or above their own rank.

The role guards in `test_user_management.py` stop someone changing a ROLE.
They did not stop an admin taking over the account that holds one: setting
the owner's password, or changing the owner's email and using "forgot
password", signs the admin in as the owner. These pin the target-rank guard
that closes that, and the permission rules around it.

The handlers run for real against in-memory SQLite, as in `test_platform.py`.
The refusals raise before any audit or session write, so only the portable
tables are needed; the one success path stubs those two writes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.api.v1 import users
from suliko.core.errors import PermissionDeniedError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.plans import TenantPlan, effective_permissions
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role, User, UserPermissionOverride
from suliko.security.passwords import hash_password
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

P = Permission
TENANT = 1
OWNER, CO_OWNER, ADMIN, OTHER_ADMIN, MANAGER, STAFF, SUPERUSER = 1, 2, 3, 4, 5, 6, 7

PORTABLE_TABLES = [Tenant.__table__, User.__table__, UserPermissionOverride.__table__]


# ── The guard itself ────────────────────────────────────────────────────────


def _actor(role: Role, user_id: int = 100) -> Any:
    return SimpleNamespace(user_id=user_id, role=role)


def _row(role: Role, user_id: int = 200) -> Any:
    return SimpleNamespace(id=user_id, role=role)


@pytest.mark.parametrize(
    ("actor", "target"),
    [
        (Role.ADMIN, Role.OWNER),
        (Role.ADMIN, Role.ADMIN),
        (Role.MANAGER, Role.MANAGER),
        (Role.MANAGER, Role.ADMIN),
        (Role.STAFF, Role.OWNER),
    ],
)
def test_cannot_act_on_an_equal_or_higher_rank(actor: Role, target: Role) -> None:
    with pytest.raises(PermissionDeniedError):
        users._guard_target(_actor(actor), _row(target))


@pytest.mark.parametrize(
    ("actor", "target"),
    [
        (Role.OWNER, Role.OWNER),  # co-owners manage each other
        (Role.OWNER, Role.ADMIN),
        (Role.ADMIN, Role.MANAGER),
        (Role.MANAGER, Role.STAFF),  # a manager handed users.manage
    ],
)
def test_can_act_below_own_rank(actor: Role, target: Role) -> None:
    users._guard_target(_actor(actor), _row(target))


@pytest.mark.parametrize("actor", [Role.OWNER, Role.SUPERUSER])
def test_a_superuser_row_is_never_touched_from_a_tenant_screen(actor: Role) -> None:
    """The CLI bootstraps the superuser inside an ordinary tenant, so without
    this that tenant's owner could reset the platform account's password."""
    with pytest.raises(PermissionDeniedError):
        users._guard_target(_actor(actor), _row(Role.SUPERUSER))


def test_acting_on_yourself_is_left_to_the_handlers() -> None:
    users._guard_target(_actor(Role.ADMIN, 5), _row(Role.ADMIN, 5))


@pytest.mark.parametrize("handler", ["update_user", "reset_password", "delete_user"])
def test_every_handler_that_acts_on_a_user_runs_the_guard(handler: str) -> None:
    import inspect

    assert "_guard_target(session, row)" in inspect.getsource(getattr(users, handler))


# ── Against a database ──────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=PORTABLE_TABLES))

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        with bypass_tenant_scope():
            session.add(
                Tenant(
                    id=TENANT,
                    slug="acme",
                    display_name="Acme",
                    status=TenantStatus.ACTIVE,
                    plan="bureau",
                    locale="ka",
                )
            )
            pw = hash_password("x" * 12)
            for user_id, role in [
                (OWNER, Role.OWNER),
                (CO_OWNER, Role.OWNER),
                (ADMIN, Role.ADMIN),
                (OTHER_ADMIN, Role.ADMIN),
                (MANAGER, Role.MANAGER),
                (STAFF, Role.STAFF),
                (SUPERUSER, Role.SUPERUSER),
            ]:
                session.add(
                    User(
                        id=user_id,
                        tenant_id=TENANT,
                        username=f"u{user_id}",
                        email=f"u{user_id}@acme.ge",
                        full_name=f"User {user_id}",
                        password_hash=pw,
                        role=role,
                        is_active=True,
                    )
                )
            await session.commit()
        with tenant_scope(TENANT):
            yield session

    await engine.dispose()


def _session(user_id: int, role: Role, **overrides: Any) -> AuthenticatedSession:
    session = AuthenticatedSession(
        session_id=1,
        user_id=user_id,
        username=f"u{user_id}",
        full_name=f"User {user_id}",
        email=f"u{user_id}@acme.ge",
        role=role,
        tenant_id=TENANT,
        tenant_slug="acme",
        tenant_name="Acme",
        plan=TenantPlan.BUREAU,
        onboarding_required=False,
        must_change_password=False,
        has_mfa=False,
        permissions=effective_permissions(role, TenantPlan.BUREAU),
        mfa_satisfied_at=None,
        impersonated_by_user_id=None,
    )
    return replace(session, **overrides) if overrides else session


async def test_an_admin_cannot_set_the_owners_password(db: AsyncSession) -> None:
    """The takeover itself: set it, sign in as the owner, own the tenant."""
    with pytest.raises(PermissionDeniedError):
        await users.reset_password(
            OWNER,
            users.PasswordReset(password="a-brand-new-password"),
            db,
            _session(ADMIN, Role.ADMIN),
            None,
        )


async def test_an_admin_cannot_redirect_the_owners_email(db: AsyncSession) -> None:
    """The quieter version: change the address, then use "forgot password"."""
    with pytest.raises(PermissionDeniedError):
        await users.update_user(
            OWNER,
            users.UserUpdate(email="attacker@example.com"),
            db,
            _session(ADMIN, Role.ADMIN),
            None,
        )
    owner = (await db.execute(select(User).where(User.id == OWNER))).scalar_one()
    assert owner.email == f"u{OWNER}@acme.ge"


async def test_an_admin_cannot_deactivate_or_delete_another_admin(db: AsyncSession) -> None:
    admin = _session(ADMIN, Role.ADMIN)
    with pytest.raises(PermissionDeniedError):
        await users.update_user(OTHER_ADMIN, users.UserUpdate(is_active=False), db, admin, None)
    with pytest.raises(PermissionDeniedError):
        await users.delete_user(OTHER_ADMIN, db, admin, None)


async def test_nobody_in_the_tenant_can_touch_the_superuser(db: AsyncSession) -> None:
    with pytest.raises(PermissionDeniedError):
        await users.reset_password(
            SUPERUSER,
            users.PasswordReset(password="a-brand-new-password"),
            db,
            _session(OWNER, Role.OWNER),
            None,
        )


async def test_you_cannot_deactivate_yourself(db: AsyncSession) -> None:
    with pytest.raises(ValidationError, match="your own account"):
        await users.update_user(
            ADMIN, users.UserUpdate(is_active=False), db, _session(ADMIN, Role.ADMIN), None
        )


async def test_create_user_cannot_restore_a_revoked_permission(db: AsyncSession) -> None:
    """An admin the owner stripped of `finance.refund` must not be able to mint
    a fresh admin — with a password of their own choosing — to get it back."""
    stripped = _session(
        ADMIN,
        Role.ADMIN,
        permissions=effective_permissions(Role.ADMIN, TenantPlan.BUREAU, {"finance.refund": False}),
    )
    with pytest.raises(ValidationError, match=r"finance\.refund"):
        await users.create_user(
            users.UserCreate(
                username="sockpuppet",
                email="sock@acme.ge",
                full_name="Sock Puppet",
                password="a-long-enough-password",
                role=Role.ADMIN,
            ),
            db,
            stripped,
            None,
        )


async def test_a_promotion_cannot_hand_out_what_the_editor_lacks(db: AsyncSession) -> None:
    """Promoting staff to admin grants the admin bundle; an editor without
    `finance.refund` cannot be the one to do it."""
    stripped = _session(
        OWNER,
        Role.OWNER,
        permissions=effective_permissions(Role.OWNER, TenantPlan.BUREAU, {"finance.refund": False}),
    )
    with pytest.raises(ValidationError, match=r"finance\.refund"):
        await users.update_user(STAFF, users.UserUpdate(role=Role.ADMIN), db, stripped, None)


async def test_saving_access_keeps_what_the_editor_does_not_hold(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form disables boxes the editor lacks, and disabled boxes are never
    submitted — so their absence must not read as "revoke"."""

    async def _nothing(*_args: Any, **_kwargs: Any) -> Any:
        return None

    async def _no_invites(*_args: Any, **_kwargs: Any) -> dict[int, Any]:
        return {}

    monkeypatch.setattr(users, "revoke_all_for_user", _nothing)
    monkeypatch.setattr(users, "_invite_status_for", _no_invites)
    monkeypatch.setattr("suliko.core.audit.record", _nothing)

    # The owner once granted this staff member finance.refund.
    db.add(UserPermissionOverride(user_id=STAFF, permission="finance.refund", granted=True))
    await db.flush()

    # An admin stripped of finance.refund edits them, ticking one new box.
    admin = _session(
        ADMIN,
        Role.ADMIN,
        permissions=effective_permissions(Role.ADMIN, TenantPlan.BUREAU, {"finance.refund": False}),
    )
    staff_now = effective_permissions(Role.STAFF, TenantPlan.BUREAU, {"finance.refund": True})
    submitted = sorted(
        p.value for p in staff_now | {P.FINANCE_READ} if p is not P.FINANCE_REFUND
    )
    out = await users.update_user(
        STAFF, users.UserUpdate(permissions=submitted), db, admin, None
    )

    assert P.FINANCE_REFUND.value in out.permissions
    assert P.FINANCE_READ.value in out.permissions
