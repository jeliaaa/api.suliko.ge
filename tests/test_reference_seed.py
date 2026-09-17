"""Starter catalogues for a new organisation.

A brand-new organisation with no document types cannot create its first
order: the form has nothing to offer. So self-signup seeds the catalogues —
and deliberately NOT the prices, which are a business decision.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.reference_seed import (
    DOCUMENT_TYPES,
    LANGUAGES,
    STARTER_RATES,
    seed_reference_data,
)
from suliko.models.reference import DocumentType, Language, LanguagePairPrice
from suliko.models.tenant import Tenant, TenantStatus

TABLES = [Tenant.__table__, Language.__table__, DocumentType.__table__, LanguagePairPrice.__table__]
ACME, GLOBEX = 1, 2


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        with bypass_tenant_scope():
            session.add_all(
                [
                    Tenant(id=ACME, slug="acme", display_name="Acme", status=TenantStatus.TRIAL),
                    Tenant(
                        id=GLOBEX, slug="globex", display_name="Globex", status=TenantStatus.ACTIVE
                    ),
                ]
            )
            await session.commit()
        yield session
    await engine.dispose()


async def _count(db: AsyncSession, model: type, tenant_id: int) -> int:
    with bypass_tenant_scope():
        return int(
            await db.scalar(
                select(func.count()).select_from(model).where(model.tenant_id == tenant_id)  # type: ignore[attr-defined]
            )
            or 0
        )


async def test_a_new_organisation_gets_the_catalogues(db: AsyncSession) -> None:
    with tenant_scope(ACME):
        result = await seed_reference_data(db)
        await db.commit()

    assert await _count(db, Language, ACME) == len(LANGUAGES)
    assert await _count(db, DocumentType, ACME) == len(DOCUMENT_TYPES)
    assert result.languages_added == len(LANGUAGES)


async def test_prices_are_not_seeded_by_default(db: AsyncSession) -> None:
    """Prices are the organisation's decision. The starter list is a partial
    copy of one bureau's rates that does not even include ka→en."""
    with tenant_scope(ACME):
        result = await seed_reference_data(db)
        await db.commit()

    assert await _count(db, LanguagePairPrice, ACME) == 0
    assert result.rates_added == 0


async def test_prices_are_seeded_only_when_asked(db: AsyncSession) -> None:
    with tenant_scope(ACME):
        await seed_reference_data(db, with_rates=True)
        await db.commit()

    assert await _count(db, LanguagePairPrice, ACME) == len(STARTER_RATES)


async def test_seeding_twice_adds_nothing(db: AsyncSession) -> None:
    with tenant_scope(ACME):
        await seed_reference_data(db)
        await db.commit()
        again = await seed_reference_data(db)
        await db.commit()

    assert again.languages_added == 0
    assert again.document_types_added == 0
    assert await _count(db, Language, ACME) == len(LANGUAGES)


async def test_seeding_one_organisation_leaves_the_other_empty(db: AsyncSession) -> None:
    with tenant_scope(ACME):
        await seed_reference_data(db)
        await db.commit()

    assert await _count(db, Language, GLOBEX) == 0
    assert await _count(db, DocumentType, GLOBEX) == 0


def test_signup_seeds_catalogues_but_not_prices() -> None:
    from suliko.api.v1 import auth

    source = inspect.getsource(auth.signup)
    assert "seed_reference_data(db)" in source
    assert "with_rates=True" not in source
