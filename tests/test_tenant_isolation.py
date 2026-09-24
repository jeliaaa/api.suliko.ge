"""Tenant isolation — the load-bearing test of the whole product.

If any of these fails, one partner bureau can see another's clients, orders,
or bank details. Nothing else in this repo matters more.

These run against in-memory SQLite deliberately: they exercise the ORM filter
(layer 2 of the three in ``suliko.db.tenancy``), which is pure SQLAlchemy and
needs no service. That keeps the most important test in the suite runnable in
CI with zero infrastructure.

**Layer 3 — PostgreSQL row-level security — is NOT covered here.** It cannot
be: SQLite has no RLS. See ``test_rls.py``, which requires a real Postgres and
must be run before any external tenant is onboarded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.db.base import Base, TenantScoped
from suliko.db.tenancy import (
    TenantContextError,
    bypass_tenant_scope,
    install_tenant_filter,
    tenant_scope,
)
from suliko.models.directory import Client, ClientType, Notary, Translator
from suliko.models.order import Order, Urgency
from suliko.models.reference import DocumentType
from suliko.models.tenant import Tenant, TenantStatus

# Models whose columns are portable to SQLite. The rest (audit_log, sessions —
# JSONB and INET) are covered by the Postgres-backed suite.
PORTABLE_TABLES = [
    Tenant.__table__,
    Client.__table__,
    Translator.__table__,
    Notary.__table__,
    DocumentType.__table__,
    Order.__table__,
]

ACME = 1
GLOBEX = 2


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    # Normally done by the app factory; tests construct no app.
    install_tenant_filter()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """Two tenants with overlapping data, so a leak is unmistakable."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=PORTABLE_TABLES))

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        with bypass_tenant_scope():
            session.add_all(
                [
                    Tenant(
                        id=ACME,
                        slug="acme",
                        display_name="Acme Translations",
                        status=TenantStatus.ACTIVE,
                        locale="ka",
                    ),
                    Tenant(
                        id=GLOBEX,
                        slug="globex",
                        display_name="Globex Language",
                        status=TenantStatus.ACTIVE,
                        locale="ka",
                    ),
                ]
            )
            await session.flush()

            session.add_all(
                [
                    Client(tenant_id=ACME, name="Acme Client A", client_type=ClientType.B2B),
                    Client(tenant_id=ACME, name="Acme Client B", client_type=ClientType.B2C),
                    # Same name in both tenants: a filter that matched on name
                    # instead of tenant would look like it worked.
                    Client(tenant_id=GLOBEX, name="Acme Client A", client_type=ClientType.B2B),
                    Translator(tenant_id=ACME, name="Acme Translator"),
                    Translator(tenant_id=GLOBEX, name="Globex Translator"),
                    Notary(tenant_id=ACME, name="Acme Notary", bank_iban="GE00ACME"),
                    Notary(tenant_id=GLOBEX, name="Globex Notary"),
                ]
            )
            await session.commit()

        yield session

    await engine.dispose()


# ── Reads ───────────────────────────────────────────────────────────────────


async def test_list_returns_only_own_tenant(db: AsyncSession) -> None:
    with tenant_scope(ACME):
        names = {c.name for c in (await db.execute(select(Client))).scalars()}
    assert names == {"Acme Client A", "Acme Client B"}

    with tenant_scope(GLOBEX):
        rows = (await db.execute(select(Client))).scalars().all()
    assert len(rows) == 1
    assert rows[0].tenant_id == GLOBEX


async def test_get_by_id_across_tenants_returns_none(db: AsyncSession) -> None:
    """This is what makes the API return 404 rather than 403.

    403 would confirm the row exists, which leaks across the boundary.
    """
    with tenant_scope(ACME):
        acme_client = (await db.execute(select(Client).limit(1))).scalar_one()
        stolen_id = acme_client.id

    db.expunge_all()

    with tenant_scope(GLOBEX):
        assert await db.get(Client, stolen_id) is None


async def test_count_is_scoped(db: AsyncSession) -> None:
    """Aggregates are the easiest place to leak — a dashboard total computed
    without the filter silently includes every tenant."""
    with tenant_scope(ACME):
        acme = await db.scalar(select(func.count()).select_from(select(Client).subquery()))
    with tenant_scope(GLOBEX):
        globex = await db.scalar(select(func.count()).select_from(select(Client).subquery()))

    assert acme == 2
    assert globex == 1


async def test_filtered_query_stays_scoped(db: AsyncSession) -> None:
    """A user-supplied filter must narrow within the tenant, never across it."""
    with tenant_scope(GLOBEX):
        rows = (
            (await db.execute(select(Client).where(Client.name == "Acme Client A"))).scalars().all()
        )

    assert len(rows) == 1
    assert rows[0].tenant_id == GLOBEX


@pytest.mark.parametrize("model", [Client, Translator, Notary])
async def test_every_directory_model_is_scoped(db: AsyncSession, model: type) -> None:
    with tenant_scope(ACME):
        rows = (await db.execute(select(model))).scalars().all()
    assert rows, "fixture should have seeded rows for this tenant"
    assert all(r.tenant_id == ACME for r in rows)


# ── Writes ──────────────────────────────────────────────────────────────────


async def test_insert_is_stamped_with_the_ambient_tenant(db: AsyncSession) -> None:
    with tenant_scope(GLOBEX):
        client = Client(name="New Globex Client", client_type=ClientType.B2C)
        db.add(client)
        await db.flush()
        assert client.tenant_id == GLOBEX


async def test_insert_with_a_foreign_tenant_id_is_refused(db: AsyncSession) -> None:
    """The attack: a caller smuggles tenant_id through the request body."""
    with tenant_scope(GLOBEX), pytest.raises(TenantContextError):
        db.add(Client(tenant_id=ACME, name="Smuggled", client_type=ClientType.B2C))
        await db.flush()
    await db.rollback()


async def test_insert_with_no_tenant_context_is_refused(db: AsyncSession) -> None:
    """Fails loudly rather than writing an unowned row."""
    with pytest.raises(TenantContextError):
        db.add(Client(name="Orphan", client_type=ClientType.B2C))
        await db.flush()
    await db.rollback()


async def test_cannot_reparent_a_row_into_another_tenant(db: AsyncSession) -> None:
    with bypass_tenant_scope():
        client = (
            await db.execute(select(Client).where(Client.tenant_id == ACME).limit(1))
        ).scalar_one()

    with tenant_scope(GLOBEX):
        client.name = "Hijacked"
        with pytest.raises(TenantContextError):
            await db.flush()
    await db.rollback()


async def test_orders_are_scoped(db: AsyncSession) -> None:
    with tenant_scope(ACME):
        doc_type = DocumentType(
            name_en="Passport", name_ka="პასპორტი", price_multiplier=Decimal("1")
        )
        db.add(doc_type)
        await db.flush()

        client = (await db.execute(select(Client).limit(1))).scalar_one()
        db.add(Order(client_id=client.id, order_date=date(2026, 9, 14), urgency=Urgency.STANDARD))
        await db.flush()

    with tenant_scope(GLOBEX):
        assert (await db.execute(select(Order))).scalars().all() == []


# ── The bypass ──────────────────────────────────────────────────────────────


async def test_bypass_sees_everything(db: AsyncSession) -> None:
    """Used only by platform tooling and migrations — and it must work, or
    the superuser area cannot function."""
    with bypass_tenant_scope():
        rows = (await db.execute(select(Client))).scalars().all()
    assert len({r.tenant_id for r in rows}) == 2


async def test_bypass_does_not_leak_past_its_block(db: AsyncSession) -> None:
    """A bypass that outlived its block would silently disable isolation for
    every subsequent query on the same task."""
    with bypass_tenant_scope():
        pass

    with tenant_scope(GLOBEX):
        rows = (await db.execute(select(Client))).scalars().all()
    assert all(r.tenant_id == GLOBEX for r in rows)


async def test_tenant_context_does_not_leak_between_blocks(db: AsyncSession) -> None:
    with tenant_scope(ACME):
        pass
    with tenant_scope(GLOBEX):
        rows = (await db.execute(select(Client))).scalars().all()
    assert all(r.tenant_id == GLOBEX for r in rows)


async def test_concurrent_tasks_do_not_share_tenant_context() -> None:
    """ContextVar, not a module global.

    A plain global would interleave under asyncio and hand one request another
    tenant's id — the exact bug this design exists to prevent.
    """
    import asyncio

    from suliko.db.tenancy import try_get_current_tenant_id

    observed: list[int | None] = []

    async def worker(tenant_id: int, delay: float) -> None:
        with tenant_scope(tenant_id):
            await asyncio.sleep(delay)
            observed.append(try_get_current_tenant_id())

    # Interleaved on purpose: the first to enter is the last to read.
    await asyncio.gather(worker(ACME, 0.02), worker(GLOBEX, 0.01))

    assert sorted(x for x in observed if x is not None) == [ACME, GLOBEX]


# ── Structural guard ────────────────────────────────────────────────────────


def test_every_tenant_table_inherits_tenantscoped() -> None:
    """A model with a tenant_id column that forgets the mixin is unprotected.

    The ORM filter keys on the TenantScoped class, so inheriting it is what
    subjects a model to isolation. This catches the next person who adds a
    table and copies the column but not the base class.
    """
    import suliko.models  # noqa: F401  — populates the registry

    # Three deliberate exemptions. Any OTHER model appearing here is a bug.
    #
    # AuditLog: platform-level events (tenant created, impersonation started)
    # have no tenant at all, and the superuser must be able to read across
    # tenants — so its tenant_id is a plain nullable column and access is gated
    # by the platform.audit permission instead of by the ORM filter.
    #
    # PortalTranslatorLink: the N:M row between a suliko.ge translator and a
    # bureau. The portal must read it BEFORE any tenant is bound, to find which
    # bureaus to enter; as a TenantScoped table under RLS that read would return
    # nothing. Its tenant_id is written only by the suliko.ge admin endpoint and
    # read only to choose a tenant scope. See suliko/models/portal.py.
    #
    # PortalAccountInvite: same reasoning as PortalTranslatorLink, for the same
    # reason — resolving an invite means searching across every bureau by
    # contact details before any tenant is bound. Its tenant_id is written from
    # the inviter's session, never from a request. See suliko/models/portal.py.
    exempt = {"AuditLog", "PortalTranslatorLink", "PortalAccountInvite"}

    unprotected: list[str] = []
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        table = mapper.local_table
        if table is None or "tenant_id" not in table.columns:
            continue
        if cls.__name__ in exempt:
            continue
        if not issubclass(cls, TenantScoped):
            unprotected.append(f"{cls.__name__} ({table.name})")

    assert not unprotected, (
        "these models carry tenant_id but do not inherit TenantScoped, so the "
        f"automatic filter does not apply to them: {unprotected}"
    )
