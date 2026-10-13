"""Invoice numbers: random, in a known shape, and kept once given."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from suliko.api.v1.companies import _issued_invoice
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.invoice_numbers import new_invoice_number
from suliko.models.invoice import Invoice
from suliko.models.tenant import Tenant, TenantStatus

install_tenant_filter()

SHAPE = re.compile(r"^\d{4}-[A-HJKMNP-Z2-9]{4}$")


def test_the_number_is_year_month_and_a_suffix() -> None:
    number = new_invoice_number(date(2026, 10, 13))
    assert number.startswith("2610-")
    assert SHAPE.match(number)


def test_the_suffix_varies() -> None:
    assert len({new_invoice_number(date(2026, 10, 13)) for _ in range(50)}) > 1


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[Base.metadata.tables[m.__tablename__] for m in (Tenant, Invoice)]
            )
        )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with sessionmaker() as db:
        with bypass_tenant_scope():
            for tenant_id, slug in ((1, "acme"), (2, "globex")):
                db.add(
                    Tenant(
                        id=tenant_id,
                        slug=slug,
                        display_name=slug,
                        status=TenantStatus.ACTIVE,
                        locale="ka",
                    )
                )
        await db.commit()
    yield sessionmaker
    await engine.dispose()


async def issue(
    maker: async_sessionmaker[AsyncSession], tenant: int, order: int, day: date
) -> Invoice:
    with tenant_scope(tenant):
        async with maker() as db:
            row = await _issued_invoice(db, order, day)
            await db.commit()
            return row


@pytest.mark.asyncio
async def test_the_first_view_allocates_and_later_views_read_it_back(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    first = await issue(maker, 1, 7, date(2026, 10, 13))
    again = await issue(maker, 1, 7, date(2026, 11, 2))
    assert SHAPE.match(first.number)
    assert again.number == first.number
    # The day it was first issued, not the day it was looked at again.
    assert again.issued_on == date(2026, 10, 13)


@pytest.mark.asyncio
async def test_each_order_gets_its_own_number(maker: async_sessionmaker[AsyncSession]) -> None:
    a = await issue(maker, 1, 7, date(2026, 10, 13))
    b = await issue(maker, 1, 8, date(2026, 10, 13))
    assert a.number != b.number


@pytest.mark.asyncio
async def test_a_collision_draws_again(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    drawn = iter(["2610-AAAA", "2610-AAAA", "2610-BBBB"])
    monkeypatch.setattr("suliko.api.v1.companies.new_invoice_number", lambda _day: next(drawn))
    assert (await issue(maker, 1, 1, date(2026, 10, 13))).number == "2610-AAAA"
    assert (await issue(maker, 1, 2, date(2026, 10, 13))).number == "2610-BBBB"
