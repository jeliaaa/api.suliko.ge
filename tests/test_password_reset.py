"""Password-reset tokens.

The token is the whole security boundary of account recovery: anyone holding
a live one can take the account. So what is pinned here is not "does it work"
but the four properties that make holding one useless a moment later —
single use, expiry, invalidation of its predecessors, and never being
recoverable from the database.

In-memory SQLite, like ``test_tenant_isolation.py``: ``password_reset_tokens``
has no JSONB or INET column, so it is portable, and the token lifecycle is
pure ORM. What is NOT covered here is the row-level-security policy on the
table — SQLite has no RLS.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import PasswordResetToken, Role, User
from suliko.security import reset_tokens
from suliko.security.passwords import hash_password, hash_token

PORTABLE_TABLES = [
    Tenant.__table__,
    User.__table__,
    PasswordResetToken.__table__,
]

TENANT = 1
HOUR = 3600


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
                    display_name="Acme Translations",
                    status=TenantStatus.ACTIVE,
                    locale="ka",
                )
            )
            session.add(
                User(
                    id=1,
                    tenant_id=TENANT,
                    username="nino",
                    email="nino@acme.ge",
                    full_name="Nino Beridze",
                    password_hash=hash_password("the-original-password"),
                    role=Role.STAFF,
                    is_active=True,
                )
            )
            await session.commit()
        yield session

    await engine.dispose()


async def _user(db: AsyncSession) -> User:
    with bypass_tenant_scope():
        return (await db.execute(select(User).where(User.id == 1))).scalar_one()


async def _rows(db: AsyncSession) -> list[PasswordResetToken]:
    with bypass_tenant_scope():
        result = await db.execute(select(PasswordResetToken).order_by(PasswordResetToken.id))
        return list(result.scalars().all())


# ── The token is never recoverable ──────────────────────────────────────────


async def test_only_the_hash_is_stored(db: AsyncSession) -> None:
    """A database read must not yield a usable reset link.

    This is the specific defect in the PHP app being replaced, where these are
    stored raw — so reading the table is equivalent to account takeover.
    """
    token = await reset_tokens.issue(db, await _user(db), ttl_seconds=HOUR)

    rows = await _rows(db)
    assert len(rows) == 1
    assert rows[0].token_hash != token
    assert rows[0].token_hash == hash_token(token)
    # The plaintext appears nowhere in the row.
    assert token not in str(rows[0].__dict__)


async def test_the_token_is_stamped_with_the_users_tenant(db: AsyncSession) -> None:
    await reset_tokens.issue(db, await _user(db), ttl_seconds=HOUR)
    assert (await _rows(db))[0].tenant_id == TENANT


# ── Single use ──────────────────────────────────────────────────────────────


async def test_a_token_works_once(db: AsyncSession) -> None:
    token = await reset_tokens.issue(db, await _user(db), ttl_seconds=HOUR)

    first = await reset_tokens.consume(db, token)
    assert first is not None
    assert first.id == 1

    assert await reset_tokens.consume(db, token) is None


async def test_consuming_stamps_used_at(db: AsyncSession) -> None:
    """Spent, not deleted — `used_at` is the record that the link existed."""
    token = await reset_tokens.issue(db, await _user(db), ttl_seconds=HOUR)
    await reset_tokens.consume(db, token)

    rows = await _rows(db)
    assert len(rows) == 1
    assert rows[0].used_at is not None


# ── Expiry ──────────────────────────────────────────────────────────────────


async def test_an_expired_token_is_refused(db: AsyncSession) -> None:
    token = await reset_tokens.issue(db, await _user(db), ttl_seconds=-1)
    assert await reset_tokens.consume(db, token) is None


async def test_expiry_is_set_from_the_ttl(db: AsyncSession) -> None:
    before = datetime.now(UTC)
    await reset_tokens.issue(db, await _user(db), ttl_seconds=HOUR)

    expires = (await _rows(db))[0].expires_at
    if expires.tzinfo is None:  # SQLite drops the offset; Postgres does not.
        expires = expires.replace(tzinfo=UTC)
    assert timedelta(minutes=59) < expires - before < timedelta(minutes=61)


# ── Issuing invalidates what came before ────────────────────────────────────


async def test_requesting_a_second_link_kills_the_first(db: AsyncSession) -> None:
    """Otherwise every click of "forgot password" leaves another live key to
    the account sitting in the inbox for an hour."""
    first = await reset_tokens.issue(db, await _user(db), ttl_seconds=HOUR)
    second = await reset_tokens.issue(db, await _user(db), ttl_seconds=HOUR)

    assert await reset_tokens.consume(db, first) is None
    assert await reset_tokens.consume(db, second) is not None


# ── Everything else fails the same way ──────────────────────────────────────


async def test_an_unknown_token_is_refused(db: AsyncSession) -> None:
    assert await reset_tokens.consume(db, "rst_not-a-real-token") is None


async def test_a_deactivated_user_cannot_reset(db: AsyncSession) -> None:
    """Offboarding must not be undoable with a link issued beforehand."""
    user = await _user(db)
    token = await reset_tokens.issue(db, user, ttl_seconds=HOUR)

    with bypass_tenant_scope():
        user.is_active = False
        await db.flush()

    assert await reset_tokens.consume(db, token) is None


async def test_tokens_are_unique_per_issue(db: AsyncSession) -> None:
    user = await _user(db)
    issued = {await reset_tokens.issue(db, user, ttl_seconds=HOUR) for _ in range(10)}
    assert len(issued) == 10
