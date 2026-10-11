"""Self-service two-factor enrolment.

Until this existed a factor could only be added from the server console, so no
tenant could turn two-factor on, `MFA_REQUIRE_ENROLMENT` could not be switched
on without locking every owner out, and step-up was skipped for everyone. What
is pinned here is the flow and the three places it could be a way round the
protection instead of a way into it.

In-memory SQLite; `session_scope` is pointed at it and the session-row stamp
(`mark_mfa_satisfied`, which touches an INET table) is recorded instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pyotp
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.api.v1 import mfa
from suliko.core.errors import AuthenticationError, ConflictError, MfaRequiredError
from suliko.core.ratelimit import RateLimiter
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.plans import TenantPlan, effective_permissions
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import MfaMethod, MfaRecoveryCode, Role, User
from suliko.security.passwords import hash_password
from suliko.security.sessions import AuthenticatedSession

TENANT = 1
TABLES = [Tenant.__table__, User.__table__, MfaMethod.__table__, MfaRecoveryCode.__table__]


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest_asyncio.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
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
            session.add(
                User(
                    id=1,
                    tenant_id=TENANT,
                    username="owner@acme.ge",
                    email="owner@acme.ge",
                    full_name="Owner",
                    password_hash=hash_password("x" * 12),
                    role=Role.OWNER,
                    is_active=True,
                )
            )
            await session.commit()

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with maker() as s:
            yield s
            await s.commit()

    satisfied: list[int] = []

    async def mark(_db: Any, session_id: int) -> None:
        satisfied.append(session_id)

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(mfa, "session_scope", scope)
    monkeypatch.setattr(mfa, "mark_mfa_satisfied", mark)
    monkeypatch.setattr("suliko.core.audit.record", _nothing)

    with tenant_scope(TENANT):
        async with maker() as reader:
            reader.info["satisfied"] = satisfied
            yield reader
    await engine.dispose()


def _session(**overrides: Any) -> AuthenticatedSession:
    session = AuthenticatedSession(
        session_id=42,
        user_id=1,
        username="owner@acme.ge",
        full_name="Owner",
        email="owner@acme.ge",
        role=Role.OWNER,
        tenant_id=TENANT,
        tenant_slug="acme",
        tenant_name="Acme",
        plan=TenantPlan.BUREAU,
        onboarding_required=False,
        must_change_password=False,
        has_mfa=False,
        permissions=effective_permissions(Role.OWNER, TenantPlan.BUREAU),
        mfa_satisfied_at=datetime.now(UTC),
        impersonated_by_user_id=None,
    )
    return replace(session, **overrides)


async def test_enrol_confirm_gives_recovery_codes_and_satisfies_the_session(
    db: AsyncSession,
) -> None:
    started = await mfa.enrol(_session())
    assert started.otpauth_uri.startswith("otpauth://totp/")
    assert started.qr_svg.startswith("<svg")

    # Not on until proven: an abandoned enrolment must never lock anyone out.
    assert await db.scalar(
        select(func.count()).select_from(MfaMethod).where(MfaMethod.confirmed_at.is_not(None))
    ) == 0

    code = pyotp.TOTP(started.secret).now()
    out = await mfa.confirm(mfa.CodeIn(code=code), _session(), RateLimiter(None))

    assert len(out.recovery_codes) == 10
    assert await db.scalar(
        select(func.count()).select_from(MfaMethod).where(MfaMethod.confirmed_at.is_not(None))
    ) == 1
    assert db.info["satisfied"] == [42]


async def test_a_wrong_code_does_not_switch_it_on(db: AsyncSession) -> None:
    await mfa.enrol(_session())
    with pytest.raises(AuthenticationError):
        await mfa.confirm(mfa.CodeIn(code="000000"), _session(), RateLimiter(None))


async def test_a_session_pending_only_for_enrolment_may_enrol() -> None:
    """What login hands a privileged account with no factor when enrolment is
    required. Without this it had no action it could take at all."""
    pending = _session(mfa_satisfied_at=None, has_mfa=False)
    assert await mfa._enrolling_session(pending) is pending


async def test_a_pending_challenge_cannot_be_bypassed_by_enrolling_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mfa, "get_settings", lambda: type("S", (), {"mfa_enforced": True})()
    )
    with pytest.raises(MfaRequiredError):
        await mfa._enrolling_session(_session(mfa_satisfied_at=None, has_mfa=True))


async def test_enrolling_twice_is_refused_once_on(db: AsyncSession) -> None:
    started = await mfa.enrol(_session())
    await mfa.confirm(
        mfa.CodeIn(code=pyotp.TOTP(started.secret).now()), _session(), RateLimiter(None)
    )
    with pytest.raises(ConflictError):
        await mfa.enrol(_session())


async def test_disabling_needs_a_code(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mfa, "_role_requires_it", lambda _s: False)
    started = await mfa.enrol(_session())
    await mfa.confirm(
        mfa.CodeIn(code=pyotp.TOTP(started.secret).now()), _session(), RateLimiter(None)
    )
    with pytest.raises(AuthenticationError):
        await mfa.disable(mfa.CodeIn(code="123456"), _session(), RateLimiter(None))


async def test_a_role_that_requires_it_cannot_switch_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mfa, "_role_requires_it", lambda _s: True)
    with pytest.raises(ConflictError):
        await mfa.disable(mfa.CodeIn(code="123456"), _session(), RateLimiter(None))
