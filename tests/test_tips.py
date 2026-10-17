"""First-run tips: who gets them, and the seen-state endpoints.

Run for real against in-memory SQLite, like ``test_leave_organisation.py``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from suliko.api.v1 import auth
from suliko.core.errors import ValidationError
from suliko.db.base import Base
from suliko.domain.plans import TenantPlan, effective_permissions
from suliko.domain.tips import TIP_IDS, apply_update, seen_tips, tips_enabled
from suliko.models.user import Account, Role
from suliko.security.sessions import AuthenticatedSession

SINCE = datetime(2026, 10, 20, tzinfo=UTC)


def _account(created: datetime | None, seen: list[str] | None = None) -> Account:
    account = Account(email="a@b.ge", full_name="A", password_hash="x")
    account.created_at = created  # type: ignore[assignment]
    account.tips_seen = seen or []
    return account


def _enabled(account: Account | None, **kwargs: bool) -> bool:
    options = {"onboarding_required": False, "impersonated": False} | kwargs
    return tips_enabled(account, since=SINCE, **options)


def test_a_new_account_gets_tips() -> None:
    assert _enabled(_account(SINCE + timedelta(days=1)))


def test_an_account_from_before_the_release_does_not() -> None:
    assert not _enabled(_account(SINCE - timedelta(seconds=1)))


def test_a_naive_timestamp_is_read_as_utc() -> None:
    assert _enabled(_account(datetime(2026, 10, 21, tzinfo=UTC).replace(tzinfo=None)))


def test_not_before_the_plan_is_chosen() -> None:
    assert not _enabled(_account(SINCE + timedelta(days=1)), onboarding_required=True)


def test_not_while_acting_as_someone_else() -> None:
    assert not _enabled(_account(SINCE + timedelta(days=1)), impersonated=True)


def test_no_account_no_tips() -> None:
    assert not _enabled(None)


def test_seen_drops_unknown_ids_and_follows_tour_order() -> None:
    account = _account(SINCE, ["users-invite", "gone", "nav-clients"])
    assert seen_tips(account) == ["nav-clients", "users-invite"]


def test_apply_update_adds_and_resets() -> None:
    account = _account(SINCE, ["nav-clients"])
    assert apply_update(account, seen=["nav-translations"], reset=False) == [
        "nav-translations",
        "nav-clients",
    ]
    assert apply_update(account, seen=["users-invite"], reset=True) == []


def test_tip_ids_are_unique_and_plain() -> None:
    assert len(set(TIP_IDS)) == len(TIP_IDS)
    assert all(tip == tip.lower() and " " not in tip for tip in TIP_IDS)


@pytest_asyncio.fixture
async def db():  # type: ignore[no-untyped-def]
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[Account.__table__]))
    session = async_sessionmaker(engine, expire_on_commit=False)()
    session.add(
        Account(
            id=1,
            email="a@b.ge",
            full_name="A",
            password_hash="x",
            created_at=SINCE + timedelta(days=1),
        )
    )
    await session.commit()
    yield session
    await session.close()
    await engine.dispose()


def _session(*, impersonated: bool = False, onboarding: bool = False) -> AuthenticatedSession:
    return AuthenticatedSession(
        session_id=1,
        user_id=1,
        username="u1",
        full_name="U",
        email="a@b.ge",
        role=Role.OWNER,
        tenant_id=1,
        tenant_slug="acme",
        tenant_name="Acme",
        plan=TenantPlan.BUREAU,
        onboarding_required=onboarding,
        must_change_password=False,
        has_mfa=False,
        permissions=effective_permissions(Role.OWNER, TenantPlan.BUREAU),
        mfa_satisfied_at=None,
        impersonated_by_user_id=2 if impersonated else None,
    )


async def test_endpoints_save_and_reset(db) -> None:  # type: ignore[no-untyped-def]
    session = replace(_session(), account_id=1)
    assert await auth.get_tips(session, db) == auth.TipsState(enabled=True, seen=[])
    state = await auth.update_tips(auth.TipsUpdate(seen=["nav-clients"]), session, db)
    assert state.seen == ["nav-clients"]
    await db.commit()
    assert (await auth.get_tips(session, db)).seen == ["nav-clients"]
    state = await auth.update_tips(auth.TipsUpdate(reset=True), session, db)
    assert state.seen == []


async def test_an_unknown_tip_is_refused(db) -> None:  # type: ignore[no-untyped-def]
    session = replace(_session(), account_id=1)
    with pytest.raises(ValidationError):
        await auth.update_tips(auth.TipsUpdate(seen=["made-up"]), session, db)


async def test_a_superuser_acting_as_someone_cannot_change_tips(db) -> None:  # type: ignore[no-untyped-def]
    session = replace(_session(impersonated=True), account_id=1)
    with pytest.raises(ValidationError):
        await auth.update_tips(auth.TipsUpdate(seen=["nav-clients"]), session, db)


async def test_disabled_before_the_plan_is_chosen(db) -> None:  # type: ignore[no-untyped-def]
    session = replace(_session(onboarding=True), account_id=1)
    assert (await auth.get_tips(session, db)).enabled is False
