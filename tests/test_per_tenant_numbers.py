"""Orders and clients are numbered 1, 2, 3 within their own bureau.

The id is shared by every tenant; a new bureau's third order read "#13"
(Suliko Office UX audit, F41). The number is drawn by an ORM hook on insert
(suliko.db.numbering), backed on PostgreSQL by an advisory lock and by the
trigger revision 0016 installs. Here, on SQLite, the counting itself.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope
from suliko.models.directory import Client, ClientType
from suliko.models.tenant import Tenant, TenantStatus

ACME, GLOBEX = 1, 2


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
            await session.commit()
            yield session
    await engine.dispose()


def _client(tenant_id: int, name: str) -> Client:
    return Client(tenant_id=tenant_id, name=name, client_type=ClientType.B2C)


async def test_each_bureau_counts_from_one(db: AsyncSession) -> None:
    first = _client(ACME, "a")
    db.add(first)
    await db.flush()
    other = _client(GLOBEX, "b")
    db.add(other)
    await db.flush()
    assert (first.number, other.number) == (1, 1)


async def test_rows_flushed_together_count_on_from_each_other(db: AsyncSession) -> None:
    rows = [_client(ACME, "a"), _client(ACME, "b"), _client(GLOBEX, "c"), _client(ACME, "d")]
    db.add_all(rows)
    await db.flush()
    assert [r.number for r in rows] == [1, 2, 1, 3]


async def test_a_later_insert_continues_from_the_highest(db: AsyncSession) -> None:
    db.add_all([_client(ACME, "a"), _client(ACME, "b")])
    await db.flush()
    later = _client(ACME, "c")
    db.add(later)
    await db.flush()
    assert later.number == 3


async def test_a_number_given_explicitly_is_kept(db: AsyncSession) -> None:
    imported = _client(ACME, "imported")
    imported.number = 40
    db.add(imported)
    await db.flush()
    next_one = _client(ACME, "next")
    db.add(next_one)
    await db.flush()
    assert (imported.number, next_one.number) == (40, 41)
