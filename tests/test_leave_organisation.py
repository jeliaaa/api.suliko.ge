"""Closing your own access to an organisation (``POST /auth/leave``).

Run for real against in-memory SQLite, like ``test_user_rank_guard.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.api.v1 import auth
from suliko.core.errors import ConflictError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.plans import TenantPlan, effective_permissions
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Account, Role, User
from suliko.security.passwords import hash_password
from suliko.security.sessions import AuthenticatedSession

TENANT = 1
PASSWORD = "correct horse battery"


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def record(_db: object, _session: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("suliko.core.audit.record", record)
    return calls


def _session(user_id: int, role: Role, account_id: int | None = None) -> AuthenticatedSession:
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
    return replace(session, account_id=account_id)


async def make_db(users: list[tuple[int, Role, bool]]) -> tuple[Any, AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[Tenant.__table__, Account.__table__, User.__table__]
            )
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    db = maker()
    with bypass_tenant_scope():
        db.add(
            Tenant(
                id=TENANT,
                slug="acme",
                display_name="Acme",
                status=TenantStatus.ACTIVE,
                plan="bureau",
                locale="ka",
            )
        )
        db.add(Account(
            id=1, email="u1@acme.ge", full_name="User 1", password_hash=hash_password(PASSWORD)
        ))
        await db.flush()
        for user_id, role, active in users:
            db.add(
                User(
                    id=user_id,
                    tenant_id=TENANT,
                    account_id=1 if user_id == 1 else None,
                    username=f"u{user_id}",
                    email=f"u{user_id}@acme.ge",
                    full_name=f"User {user_id}",
                    password_hash=hash_password("x" * 12),
                    role=role,
                    is_active=active,
                )
            )
        await db.commit()
    return engine, db


@pytest_asyncio.fixture
async def leave() -> AsyncIterator[Any]:
    """`leave(users, acting_id, role, password)`: runs the handler, returns the db."""
    opened: list[tuple[Any, AsyncSession]] = []

    async def run(
        users: list[tuple[int, Role, bool]],
        acting: int,
        role: Role,
        password: str | None = PASSWORD,
        account_id: int | None = None,
    ) -> AsyncSession:
        engine, db = await make_db(users)
        opened.append((engine, db))
        with tenant_scope(TENANT):
            try:
                await auth.leave_organisation(
                    auth.LeaveRequest(current_password=password),
                    _session(acting, role, account_id),
                    db,
                )
            finally:
                await db.commit()
        return db

    yield run
    for engine, db in opened:
        await db.close()
        await engine.dispose()


async def active(db: AsyncSession, user_id: int) -> bool:
    with tenant_scope(TENANT):
        row = await db.get(User, user_id)
        assert row is not None
        await db.refresh(row)
        return row.is_active


@pytest.mark.asyncio
async def test_a_member_can_leave_and_is_switched_off_not_deleted(leave: Any) -> None:
    db = await leave([(1, Role.OWNER, True), (2, Role.STAFF, True)], 2, Role.STAFF)
    assert await active(db, 2) is False
    assert await active(db, 1) is True


@pytest.mark.asyncio
async def test_the_only_owner_cannot_leave_others_behind(leave: Any) -> None:
    with pytest.raises(ConflictError):
        await leave([(1, Role.OWNER, True), (2, Role.STAFF, True)], 1, Role.OWNER)


@pytest.mark.asyncio
async def test_a_co_owner_can_leave(leave: Any) -> None:
    db = await leave([(1, Role.OWNER, True), (2, Role.OWNER, True)], 1, Role.OWNER)
    assert await active(db, 1) is False


@pytest.mark.asyncio
async def test_a_sole_user_can_leave(leave: Any) -> None:
    """A freelancer's own workspace has one person in it."""
    db = await leave([(1, Role.OWNER, True)], 1, Role.OWNER)
    assert await active(db, 1) is False


@pytest.mark.asyncio
async def test_people_already_switched_off_do_not_count_as_staying(leave: Any) -> None:
    db = await leave([(1, Role.OWNER, True), (2, Role.STAFF, False)], 1, Role.OWNER)
    assert await active(db, 1) is False


@pytest.mark.asyncio
async def test_an_account_with_a_password_must_give_it(leave: Any) -> None:
    for password in (None, "wrong"):
        with pytest.raises(ValidationError):
            await leave([(1, Role.STAFF, True)], 1, Role.STAFF, password, account_id=1)


@pytest.mark.asyncio
async def test_the_right_password_is_enough(leave: Any) -> None:
    db = await leave([(1, Role.STAFF, True)], 1, Role.STAFF, PASSWORD, account_id=1)
    assert await active(db, 1) is False


@pytest.mark.asyncio
async def test_it_is_audited(leave: Any, audit: list[dict[str, Any]]) -> None:
    await leave([(1, Role.OWNER, True), (2, Role.STAFF, True)], 2, Role.STAFF)
    assert [c["action"] for c in audit] == ["user.left"]
