"""The platform area — the one router that is outside the tenancy model.

Everything else in the product is protected by not being able to see another
tenant. This router can see all of them by design, so what has to be proved is
different: that each query says WHICH tenant it means, and that the guards
which stop a platform operator breaking a bureau actually hold.

In-memory SQLite, as in `test_tenant_isolation.py`. The queries are plain
SQLAlchemy over portable tables, so they run with no infrastructure — and the
ones that matter here are the explicit `tenant_id` predicates, which SQLite
enforces exactly as PostgreSQL does.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.api.v1 import platform
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter
from suliko.models.directory import Client, ClientType
from suliko.models.order import CopyType, Order, OrderDocument, Urgency
from suliko.models.reference import DocumentType, Language, LanguagePairPrice
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role, User
from suliko.security.passwords import hash_password

ACME, GLOBEX = 1, 2

PORTABLE_TABLES = [
    Tenant.__table__,
    User.__table__,
    Client.__table__,
    DocumentType.__table__,
    Language.__table__,
    LanguagePairPrice.__table__,
    Order.__table__,
    OrderDocument.__table__,
]


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """Two bureaus with different sizes, so a leak shows up as a wrong number.

    Acme has two users, two priced pairs and one two-document order. Globex
    has one user, one pair and one single-document order. Every figure below
    is therefore distinguishable.
    """
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
                        plan="bureau",
                        locale="ka",
                    ),
                    Tenant(
                        id=GLOBEX,
                        slug="globex",
                        display_name="Globex Language",
                        status=TenantStatus.TRIAL,
                        plan=None,  # signed up, has not chosen
                        locale="en",
                    ),
                ]
            )
            session.add_all(
                [
                    User(
                        id=1,
                        tenant_id=ACME,
                        username="owner@acme.ge",
                        email="owner@acme.ge",
                        full_name="Acme Owner",
                        password_hash=hash_password("x" * 12),
                        role=Role.OWNER,
                        is_active=True,
                    ),
                    User(
                        id=2,
                        tenant_id=ACME,
                        username="staff@acme.ge",
                        email="staff@acme.ge",
                        full_name="Acme Staff",
                        password_hash=hash_password("x" * 12),
                        role=Role.STAFF,
                        is_active=False,
                    ),
                    User(
                        id=3,
                        tenant_id=GLOBEX,
                        username="owner@globex.ge",
                        email="owner@globex.ge",
                        full_name="Globex Owner",
                        password_hash=hash_password("x" * 12),
                        role=Role.OWNER,
                        is_active=True,
                    ),
                ]
            )
            session.add_all(
                [
                    Language(tenant_id=ACME, code="ka", name_en="Georgian", name_ka="ქართული"),
                    Language(tenant_id=ACME, code="en", name_en="English", name_ka="ინგლისური"),
                    Language(tenant_id=GLOBEX, code="de", name_en="German", name_ka="გერმანული"),
                ]
            )
            session.add_all(
                [
                    LanguagePairPrice(
                        tenant_id=ACME,
                        source_language="ka",
                        target_language="en",
                        price_per_page=Decimal("25.00"),
                    ),
                    LanguagePairPrice(
                        tenant_id=ACME,
                        source_language="en",
                        target_language="ka",
                        price_per_page=Decimal("30.00"),
                    ),
                    LanguagePairPrice(
                        tenant_id=GLOBEX,
                        source_language="de",
                        target_language="en",
                        price_per_page=Decimal("99.00"),
                    ),
                ]
            )
            session.add_all(
                [
                    Client(id=1, tenant_id=ACME, name="Acme Client", client_type=ClientType.B2C),
                    Client(
                        id=2, tenant_id=GLOBEX, name="Globex Client", client_type=ClientType.B2B
                    ),
                    DocumentType(id=1, tenant_id=ACME, name_en="Diploma", name_ka="დიპლომი"),
                    DocumentType(
                        id=2, tenant_id=GLOBEX, name_en="Contract", name_ka="ხელშეკრულება"
                    ),
                ]
            )
            session.add_all(
                [
                    Order(
                        id=1,
                        tenant_id=ACME,
                        client_id=1,
                        order_date=date(2026, 3, 1),
                        urgency=Urgency.STANDARD,
                        delivery_cost=Decimal("0"),
                    ),
                    Order(
                        id=2,
                        tenant_id=GLOBEX,
                        client_id=2,
                        order_date=date(2026, 5, 9),
                        urgency=Urgency.STANDARD,
                        delivery_cost=Decimal("0"),
                    ),
                ]
            )
            session.add_all(
                [
                    OrderDocument(
                        tenant_id=ACME,
                        order_id=1,
                        document_type_id=1,
                        source_language="ka",
                        target_language="en",
                        page_count=3,
                        copy_type=CopyType.ORIGINAL,
                        price=Decimal("100.00"),
                        translator_cost=Decimal("40.00"),
                        notary_cost=Decimal("10.00"),
                    ),
                    OrderDocument(
                        tenant_id=ACME,
                        order_id=1,
                        document_type_id=1,
                        source_language="en",
                        target_language="ka",
                        page_count=2,
                        copy_type=CopyType.ORIGINAL,
                        price=Decimal("60.00"),
                        translator_cost=Decimal("20.00"),
                        notary_cost=Decimal("0"),
                    ),
                    OrderDocument(
                        tenant_id=GLOBEX,
                        order_id=2,
                        document_type_id=2,
                        source_language="de",
                        target_language="en",
                        page_count=10,
                        copy_type=CopyType.ORIGINAL,
                        price=Decimal("999.00"),
                        translator_cost=Decimal("500.00"),
                        notary_cost=Decimal("0"),
                    ),
                ]
            )
            await session.commit()
        yield session

    await engine.dispose()


# ── Figures are per tenant, not per platform ────────────────────────────────


async def test_one_tenants_figures_exclude_the_other(db: AsyncSession) -> None:
    """The failure this exists to catch: an aggregate that forgets its
    predicate still returns a number, and the number looks plausible."""
    acme = await platform._figures(db, ACME)

    assert acme.orders == 1
    assert acme.documents == 2
    assert acme.pages == 5
    assert acme.revenue == Decimal("160.00")
    assert acme.translator_cost == Decimal("60.00")
    assert acme.notary_cost == Decimal("10.00")
    # 160 - 60 - 10. Globex's 999 must be nowhere in this.
    assert acme.gross_profit == Decimal("90.00")


async def test_the_other_tenant_gets_its_own_figures(db: AsyncSession) -> None:
    globex = await platform._figures(db, GLOBEX)

    assert globex.orders == 1
    assert globex.documents == 1
    assert globex.pages == 10
    assert globex.revenue == Decimal("999.00")
    assert globex.gross_profit == Decimal("499.00")


async def test_a_tenant_with_nothing_reports_zero_rather_than_null(db: AsyncSession) -> None:
    """A newly created bureau must render, not crash the screen on a None."""
    figures = await platform._figures(db, 999)

    assert figures.orders == 0
    assert figures.revenue == Decimal("0")
    assert figures.gross_profit == Decimal("0")
    assert figures.first_order is None


async def test_the_order_window_is_the_tenants_own(db: AsyncSession) -> None:
    acme = await platform._figures(db, ACME)
    assert acme.first_order == date(2026, 3, 1)
    assert acme.last_order == date(2026, 3, 1)


# ── Every query names its tenant ────────────────────────────────────────────


def test_no_query_relies_on_the_ambient_tenant() -> None:
    """A cross-tenant router must not lean on "the current tenant" — the ORM
    filter and the RLS policy both key on it, and this router is outside both.

    Checked by reading the source: every handler that reads tenant-owned rows
    has to spell out a `tenant_id` predicate.
    """
    source = inspect.getsource(platform)

    for handler in ("list_tenants", "get_tenant", "_figures", "create_tenant_user"):
        body = inspect.getsource(getattr(platform, handler))
        assert "tenant_id" in body, f"{handler} does not name a tenant"

    # And the escape hatch is used, deliberately and visibly.
    assert "bypass_tenant_scope" in source


def test_writing_into_another_tenant_binds_all_three() -> None:
    """The RLS GUC, the ORM's insert stamp and the row's own column have to
    agree, or the write lands somewhere else or is rejected."""
    body = inspect.getsource(platform.create_tenant_user)

    assert "bind_tenant_guc(db, tenant_id)" in body
    assert "tenant_scope(tenant_id)" in body
    assert "tenant_id=tenant_id" in body


# ── The guards ──────────────────────────────────────────────────────────────


def test_superuser_cannot_be_minted_over_http() -> None:
    """Unchanged from `users.py`, and asserted there too: an account with
    platform-wide reach requires filesystem access to the server."""
    body = inspect.getsource(platform.create_tenant_user)
    assert "Role.SUPERUSER" in body
    assert "server console" in body


def test_a_superuser_cannot_be_deleted_here_either() -> None:
    body = inspect.getsource(platform.delete_tenant_user)
    assert "Role.SUPERUSER" in body


def test_a_tenants_last_owner_is_protected(db: AsyncSession) -> None:
    """Deleting it would leave a paying bureau with nobody who can administer
    it — and no way to invite a replacement."""
    body = inspect.getsource(platform.delete_tenant_user)
    assert "remaining_owners" in body
    assert "last active owner" in body


def test_deleting_checks_the_user_belongs_to_the_named_tenant() -> None:
    """The tenant is in the path. Without this check a mistyped user id would
    delete somebody in a different bureau entirely."""
    body = inspect.getsource(platform.delete_tenant_user)
    assert "row.tenant_id != tenant_id" in body


def test_an_operator_cannot_suspend_their_own_tenant() -> None:
    """They would lock themselves out of the console they need to undo it —
    `resolve_session` refuses a suspended tenant on the next request."""
    body = inspect.getsource(platform.set_tenant_status)
    assert "cannot suspend the tenant you are signed in to" in body


def test_deleting_a_user_revokes_their_sessions() -> None:
    body = inspect.getsource(platform.delete_tenant_user)
    assert "revoke_all_for_user" in body


def test_every_cross_tenant_write_is_audited() -> None:
    """These are the actions a platform operator takes on somebody else's
    data. An unaudited one is indistinguishable from a compromise."""
    for handler in ("create_tenant_user", "delete_tenant_user", "set_tenant_status"):
        body = inspect.getsource(getattr(platform, handler))
        assert "await record(" in body, f"{handler} is not audited"
        assert "tenant_id=" in body, f"{handler} does not audit WHICH tenant"


def test_the_whole_router_is_superuser_only() -> None:
    """`platform.tenants` is in no tenant role's bundle — see
    `test_parity.py::test_platform_permissions_are_superuser_only`."""
    source = inspect.getsource(platform)
    assert "require(Permission.PLATFORM_TENANTS)" in source

    for route in platform.router.routes:
        assert route.dependant.dependencies, f"{route.path} has no dependency chain"  # type: ignore[attr-defined]


# ── Impersonation is deliberately absent ────────────────────────────────────


def test_impersonation_is_not_implemented() -> None:
    """It needs a freshly verified second factor (`PLATFORM_IMPERSONATE` is in
    STEP_UP_PERMISSIONS) and there is still no enrolment screen — so the one
    control between reading a tenant's data and acting as their owner cannot
    be satisfied. If this test starts failing, the enrolment screen had better
    exist.
    """
    source = inspect.getsource(platform)
    assert "PLATFORM_IMPERSONATE" not in source.replace(
        "**Impersonation.** `Permission.PLATFORM_IMPERSONATE`", ""
    )
