"""The new-client form's "may already exist" check, and phone search.

Phones are stored as typed, so the same number arrives as "+995 555 12-34-56"
one day and "555123456" the next. Both the duplicate warning and the search
compare digits to digits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.api.v1 import clients
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.models.directory import Client, ClientType
from suliko.models.tenant import Tenant, TenantStatus

ACME, GLOBEX = 1, 2


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[Tenant.__table__, Client.__table__])
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        with bypass_tenant_scope():
            for tid in (ACME, GLOBEX):
                session.add(
                    Tenant(
                        id=tid,
                        slug=f"t{tid}",
                        display_name=f"T{tid}",
                        status=TenantStatus.ACTIVE,
                        plan="bureau",
                        locale="ka",
                    )
                )
            session.add_all(
                [
                    Client(
                        id=1,
                        tenant_id=ACME,
                        name="Nino Beridze",
                        client_type=ClientType.B2C,
                        phone="+995 555 12-34-56",
                        email="Nino@Mail.ge",
                    ),
                    Client(
                        id=2,
                        tenant_id=GLOBEX,
                        name="Someone at another bureau",
                        client_type=ClientType.B2C,
                        phone="555123456",
                    ),
                ]
            )
            await session.commit()
        with tenant_scope(ACME):
            yield session
    await engine.dispose()


async def test_the_same_number_typed_differently_is_a_possible_duplicate(
    db: AsyncSession,
) -> None:
    found = await clients.possible_duplicates(db, None, phone="555123456")  # type: ignore[arg-type]
    assert [(c.id, c.matched_on) for c in found] == [(1, ["phone"])]


async def test_email_matches_regardless_of_case(db: AsyncSession) -> None:
    found = await clients.possible_duplicates(db, None, email="nino@mail.GE")  # type: ignore[arg-type]
    assert [c.id for c in found] == [1]


async def test_another_bureaus_client_is_never_a_duplicate(db: AsyncSession) -> None:
    """Client 2 has the same number but belongs to Globex."""
    found = await clients.possible_duplicates(db, None, phone="555 123 456")  # type: ignore[arg-type]
    assert [c.id for c in found] == [1]


async def test_the_list_finds_a_phone_by_its_digits(db: AsyncSession) -> None:
    page = await clients.list_clients(db, None, search="555 1234", client_type=None)  # type: ignore[arg-type]
    assert [c.id for c in page.items] == [1]
