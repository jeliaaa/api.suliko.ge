"""One account per person, many organisations — sign-in's foundations.

In-memory SQLite, like ``test_password_reset.py``: accounts, users, tenants and
the starter catalogues are portable. What is NOT covered here is PostgreSQL
row-level security, or the endpoints themselves, which open their own
database sessions (their ordering rules are pinned in ``test_tenant_access.py``).
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
from suliko.domain.accounts import (
    UNUSABLE_PASSWORD_HASH,
    create_personal_workspace,
    find_account,
    memberships,
    personal,
    revoke_account_sessions,
)
from suliko.models.reference import DocumentType, Language, LanguagePairPrice, TenantSettings
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Account, Role, User
from suliko.security import login_tickets
from suliko.security.passwords import verify_password

TABLES = [
    Tenant.__table__,
    Account.__table__,
    User.__table__,
    TenantSettings.__table__,
    Language.__table__,
    DocumentType.__table__,
    LanguagePairPrice.__table__,
]

NINO = 1
BUREAU, OTHER, SUSPENDED = 10, 11, 12


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


def _membership(
    user_id: int, tenant_id: int, account_id: int | None = NINO, **extra: object
) -> User:
    return User(
        id=user_id,
        tenant_id=tenant_id,
        account_id=account_id,
        username=f"nino{user_id}@acme.ge",
        email="nino@acme.ge",
        full_name="Nino Beridze",
        password_hash=UNUSABLE_PASSWORD_HASH,
        role=Role.STAFF,
        is_active=True,
        **extra,
    )


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        with bypass_tenant_scope():
            for tenant_id, slug, status in (
                (BUREAU, "acme", TenantStatus.ACTIVE),
                (OTHER, "beta", TenantStatus.TRIAL),
                (SUSPENDED, "gone", TenantStatus.SUSPENDED),
            ):
                session.add(
                    Tenant(
                        id=tenant_id,
                        slug=slug,
                        display_name=slug.title(),
                        status=status,
                        plan="bureau",
                        locale="ka",
                    )
                )
            session.add(
                Account(
                    id=NINO,
                    email="nino@acme.ge",
                    full_name="Nino Beridze",
                    password_hash="$argon2id$fake",
                )
            )
            await session.flush()
            session.add(_membership(1, BUREAU))
            session.add(_membership(2, OTHER))
            session.add(_membership(3, SUSPENDED))
            await session.commit()
        yield session
    await engine.dispose()


# ── Finding the account ─────────────────────────────────────────────────────


async def test_an_account_is_found_by_email_whatever_its_case(db: AsyncSession) -> None:
    account = await find_account(db, "  Nino@ACME.ge ")
    assert account is not None and account.id == NINO
    assert await find_account(db, "nobody@acme.ge") is None


# ── Which organisations the person can enter ────────────────────────────────


async def test_every_usable_organisation_is_offered(db: AsyncSession) -> None:
    slugs = [m.tenant.slug for m in await memberships(db, NINO)]
    assert sorted(slugs) == ["acme", "beta"]


async def test_a_pending_invitation_is_not_offered(db: AsyncSession) -> None:
    with bypass_tenant_scope():
        row = (await db.execute(select(User).where(User.id == 2))).scalar_one()
        row.invitation_pending = True
        await db.flush()
    assert [m.tenant.slug for m in await memberships(db, NINO)] == ["acme"]


async def test_a_deactivated_membership_is_not_offered(db: AsyncSession) -> None:
    with bypass_tenant_scope():
        row = (await db.execute(select(User).where(User.id == 1))).scalar_one()
        row.is_active = False
        await db.flush()
    assert [m.tenant.slug for m in await memberships(db, NINO)] == ["beta"]


async def test_one_organisation_is_offered_once(db: AsyncSession) -> None:
    """Imported data can hold two rows for one person in one bureau; the
    chooser must not list the bureau twice."""
    with bypass_tenant_scope():
        db.add(
            User(
                id=4,
                tenant_id=BUREAU,
                account_id=NINO,
                username="Nino@acme.ge",
                email="Nino@acme.ge",
                full_name="Nino",
                password_hash=UNUSABLE_PASSWORD_HASH,
                role=Role.STAFF,
                is_active=True,
            )
        )
        await db.flush()
    slugs = [m.tenant.slug for m in await memberships(db, NINO)]
    assert slugs.count("acme") == 1


async def test_someone_elses_rows_are_never_offered(db: AsyncSession) -> None:
    with bypass_tenant_scope():
        db.add(Account(id=2, email="giorgi@acme.ge", full_name="Giorgi", password_hash="x"))
        await db.flush()
    assert await memberships(db, 2) == []


# ── The personal account ────────────────────────────────────────────────────


async def test_there_is_no_personal_account_until_one_is_made(db: AsyncSession) -> None:
    assert personal(await memberships(db, NINO)) is None


async def test_the_personal_account_is_a_freelancer_workspace_they_own(db: AsyncSession) -> None:
    account = await db.get(Account, NINO)
    assert account is not None
    created = await create_personal_workspace(db, account)

    assert created.tenant.is_personal
    assert created.tenant.plan == "freelancer"
    assert created.user.role is Role.OWNER
    assert created.user.account_id == NINO
    # Their password is the account's; the membership row holds none.
    assert not verify_password("anything", created.user.password_hash)

    own = personal(await memberships(db, NINO))
    assert own is not None and own.tenant.id == created.tenant.id


async def test_the_personal_workspace_starts_with_document_types(db: AsyncSession) -> None:
    account = await db.get(Account, NINO)
    assert account is not None
    created = await create_personal_workspace(db, account)
    with bypass_tenant_scope():
        count = (
            await db.execute(
                select(DocumentType).where(DocumentType.tenant_id == created.tenant.id)
            )
        ).all()
    assert count


async def test_a_bureau_is_never_mistaken_for_the_personal_account(db: AsyncSession) -> None:
    """A freelancer-plan bureau someone was invited to is not theirs."""
    with bypass_tenant_scope():
        tenant = (await db.execute(select(Tenant).where(Tenant.id == OTHER))).scalar_one()
        tenant.plan = "freelancer"
        await db.flush()
    assert personal(await memberships(db, NINO)) is None


# ── Signing out everywhere ──────────────────────────────────────────────────


async def test_a_password_change_reaches_every_organisation(db: AsyncSession) -> None:
    before = datetime.now(UTC) - timedelta(seconds=1)
    await revoke_account_sessions(db, NINO)
    with bypass_tenant_scope():
        rows = (await db.execute(select(User).where(User.account_id == NINO))).scalars().all()
        for row in rows:
            await db.refresh(row)
    stamps = [row.sessions_invalid_before for row in rows]
    assert all(stamp is not None for stamp in stamps)
    assert all(stamp.replace(tzinfo=UTC) >= before for stamp in stamps if stamp)


# ── The ticket between the two sign-in steps ────────────────────────────────


def test_a_ticket_names_its_account() -> None:
    ticket = login_tickets.issue(NINO, "$hash")
    assert login_tickets.read(ticket) == NINO
    assert login_tickets.matches(ticket, "$hash")


def test_a_ticket_dies_with_the_password_it_was_issued_under() -> None:
    ticket = login_tickets.issue(NINO, "$old-hash")
    assert not login_tickets.matches(ticket, "$new-hash")


def test_a_ticket_expires() -> None:
    issued = datetime.now(UTC) - timedelta(seconds=login_tickets.TTL_SECONDS + 1)
    ticket = login_tickets.issue(NINO, "$hash", now=issued)
    with pytest.raises(login_tickets.TicketError):
        login_tickets.read(ticket)


def test_a_ticket_cannot_be_pointed_at_another_account() -> None:
    _account, expires, fingerprint, signature = login_tickets.issue(NINO, "$hash").split(".")
    forged = f"999.{expires}.{fingerprint}.{signature}"
    with pytest.raises(login_tickets.TicketError):
        login_tickets.read(forged)


@pytest.mark.parametrize("garbage", ["", "a.b.c", "x.y.z.w", "1.2.3.4.5"])
def test_garbage_is_not_a_ticket(garbage: str) -> None:
    with pytest.raises(login_tickets.TicketError):
        login_tickets.read(garbage)
