"""Order rules added in the 2026-09 review.

Money and isolation rules on the order endpoints: what an order may point at,
who sees what it cost, what counts as profit, how "today" is decided, how a
list pages, and what an urgency change does to prices someone typed in.

In-memory SQLite for the handler-level ones, as in `test_platform.py`;
rendered PostgreSQL SQL for the list query, as in `test_reports_sql.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.api.v1 import orders
from suliko.api.v1._shared import like_pattern
from suliko.core.errors import ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.clock import today_in
from suliko.domain.plans import TenantPlan, effective_permissions
from suliko.domain.pricing import DocumentPricingInput, price_document
from suliko.models.directory import Client, ClientType, Notary, Translator
from suliko.models.order import CopyType, HandoverMethod, Order, OrderDocument, Urgency
from suliko.models.reference import DocumentType, LanguagePairPrice, TenantSettings
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role
from suliko.security.sessions import AuthenticatedSession

ACME, GLOBEX = 1, 2

PORTABLE_TABLES = [
    Tenant.__table__,
    Client.__table__,
    Translator.__table__,
    Notary.__table__,
    DocumentType.__table__,
    LanguagePairPrice.__table__,
    TenantSettings.__table__,
    Order.__table__,
    OrderDocument.__table__,
]


def _session(role: Role = Role.OWNER, tenant_id: int = ACME, **kw: Any) -> AuthenticatedSession:
    session = AuthenticatedSession(
        session_id=1,
        user_id=1,
        username="u",
        full_name="U",
        email="u@acme.ge",
        role=role,
        tenant_id=tenant_id,
        tenant_slug="acme",
        tenant_name="Acme",
        plan=TenantPlan.BUREAU,
        onboarding_required=False,
        must_change_password=False,
        has_mfa=False,
        permissions=effective_permissions(role, TenantPlan.BUREAU),
        mfa_satisfied_at=None,
        impersonated_by_user_id=None,
    )
    return replace(session, **kw) if kw else session


# ── "Today" is the bureau's day ─────────────────────────────────────────────


def test_late_evening_utc_is_already_tomorrow_in_tbilisi() -> None:
    """22:00 UTC is 02:00 the next day in Tbilisi. The old UTC date put an
    order taken at 02:00 on the previous day — and on the 1st of a month,
    into the previous month's report."""
    moment = datetime(2026, 9, 30, 22, 0, tzinfo=UTC)
    assert today_in("Asia/Tbilisi", now=moment) == date(2026, 10, 1)
    assert today_in("UTC", now=moment) == date(2026, 9, 30)


def test_an_unknown_zone_falls_back_instead_of_failing() -> None:
    moment = datetime(2026, 9, 30, 22, 0, tzinfo=UTC)
    assert today_in("Mars/Olympus_Mons", now=moment) == date(2026, 10, 1)


# ── Summary figures ─────────────────────────────────────────────────────────


def _row(
    *,
    status: str | None = "new",
    documents_total: str = "100",
    gross_profit: str = "40",
    paid: str = "0",
    expenses: str = "5",
    delivery: str = "10",
    due: date | None = None,
) -> tuple[Any, ...]:
    order = SimpleNamespace(
        id=1,
        order_date=date(2026, 9, 1),
        due_date=due,
        client_id=1,
        client=SimpleNamespace(name="Nino", client_type=ClientType.B2C),
        urgency=Urgency.STANDARD,
        delivery_cost=Decimal(delivery),
    )
    return (
        order,
        status,
        Decimal(documents_total),
        Decimal("50"),
        Decimal("10"),
        Decimal(gross_profit),
        1,
        3,
        Decimal(paid),
        Decimal(expenses),
    )


def test_delivery_is_revenue_but_not_profit() -> None:
    """Decided 2026-09-24, as the PHP: the courier fee is passed through."""
    summary = orders._summary_from_row(_row(), today=date(2026, 9, 2), show_costs=True)
    assert summary.total == Decimal("110")  # documents + delivery, what is owed
    assert summary.profit == Decimal("35")  # gross 40 - expenses 5, no +10


def test_staff_without_reports_profit_do_not_get_the_profit() -> None:
    summary = orders._summary_from_row(_row(), today=date(2026, 9, 2), show_costs=False)
    assert summary.profit is None
    assert summary.total == Decimal("110")


@pytest.mark.parametrize("status", ["completed", "cancelled", "rejected"])
def test_a_closed_order_is_never_overdue(status: str) -> None:
    summary = orders._summary_from_row(
        _row(status=status, due=date(2026, 9, 1)), today=date(2026, 9, 5), show_costs=True
    )
    assert summary.is_overdue is False


def test_an_open_order_past_its_date_is_overdue() -> None:
    summary = orders._summary_from_row(
        _row(due=date(2026, 9, 1)), today=date(2026, 9, 5), show_costs=True
    )
    assert summary.is_overdue is True


def test_nothing_owed_is_paid_not_unpaid() -> None:
    assert orders._paid_state(Decimal("0"), Decimal("0")) == "paid"
    assert orders._paid_state(Decimal("100"), Decimal("0")) == "unpaid"
    assert orders._paid_state(Decimal("100"), Decimal("40")) == "partial"


# ── The list query ──────────────────────────────────────────────────────────


class _RecordingDb:
    """Captures the statements `list_orders` runs; returns an empty page."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def scalar(self, stmt: Any) -> int:
        self.statements.append(stmt)
        return 0

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)
        return SimpleNamespace(unique=lambda: SimpleNamespace(all=lambda: []))


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def _list_sql(**params: Any) -> str:
    db = _RecordingDb()
    defaults: dict[str, Any] = {
        "search": None,
        "client_type": None,
        "order_status": None,
        "language": None,
        "client_id": None,
        "translator_id": None,
        "document_type_id": None,
        "date_from": None,
        "date_to": None,
        "overdue": False,
        "due_today": False,
        "unpaid": False,
        "mine": False,
        "sort": "-date",
        "limit": 20,
        "offset": 0,
    }
    await orders.list_orders(db=db, session=_session(), **{**defaults, **params})  # type: ignore[arg-type]
    return _sql(db.statements[-1])


async def test_paging_has_a_stable_tie_break() -> None:
    """Twenty orders on one date otherwise come back in planner order, and a
    row can appear on two pages while another appears on none."""
    sql = await _list_sql(sort="-date")
    assert "ORDER BY orders.order_date DESC, orders.id DESC" in sql


async def test_the_new_filters_reach_the_query() -> None:
    sql = await _list_sql(client_id=7, translator_id=9, overdue=True, mine=True)
    assert "orders.client_id = 7" in sql
    assert "order_documents.translator_id = 9" in sql
    assert "orders.due_date <" in sql
    assert "orders.created_by_user_id = 1" in sql


async def test_search_matches_phone_digits_and_escapes_wildcards() -> None:
    sql = await _list_sql(search="+995 555-12")
    assert "replace(" in sql  # phone compared digit to digit
    assert "%99555512%" in sql


def test_like_pattern_treats_percent_and_underscore_literally() -> None:
    assert like_pattern("50%_off") == "%50\\%\\_off%"


# ── Pricing ─────────────────────────────────────────────────────────────────


def test_the_price_is_the_sum_of_the_rounded_parts() -> None:
    """Otherwise the breakdown can show two lines that do not add up to the
    price by a tetri."""
    for pages in range(1, 60):
        b = price_document(
            DocumentPricingInput(
                page_count=pages,
                base_rate_per_page=Decimal("12.345"),
                document_type_multiplier=Decimal("1.1"),
                copy_type=CopyType.NOTARY_CERTIFIED,
                urgency=Urgency.EXPRESS,
            )
        )
        assert b.price == b.translation_cost + b.notary_cost


# ── Against a database ──────────────────────────────────────────────────────


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
            for tid, slug in [(ACME, "acme"), (GLOBEX, "globex")]:
                session.add(
                    Tenant(
                        id=tid,
                        slug=slug,
                        display_name=slug,
                        status=TenantStatus.ACTIVE,
                        plan="bureau",
                        locale="ka",
                    )
                )
            session.add_all(
                [
                    Translator(id=1, tenant_id=ACME, name="Acme translator"),
                    Translator(id=2, tenant_id=GLOBEX, name="Globex translator"),
                    Notary(id=1, tenant_id=GLOBEX, name="Globex notary"),
                    Client(id=1, tenant_id=ACME, name="Nino", client_type=ClientType.B2C),
                    DocumentType(id=1, tenant_id=ACME, name_en="Passport", name_ka="პასპორტი"),
                    LanguagePairPrice(
                        tenant_id=ACME,
                        source_language="ka",
                        target_language="en",
                        price_per_page=Decimal("20.00"),
                    ),
                    Order(
                        id=1,
                        tenant_id=ACME,
                        client_id=1,
                        order_date=date(2026, 9, 1),
                        urgency=Urgency.STANDARD,
                        handover_method=HandoverMethod.SCAN,
                        delivery_cost=Decimal("0"),
                    ),
                    # Computed at standard: 2 pages x 20 = 40, translator 20.
                    OrderDocument(
                        id=1,
                        tenant_id=ACME,
                        order_id=1,
                        document_type_id=1,
                        source_language="ka",
                        target_language="en",
                        page_count=2,
                        copy_type=CopyType.ORIGINAL,
                        price=Decimal("40.00"),
                        translator_cost=Decimal("20.00"),
                        notary_cost=Decimal("0"),
                    ),
                    # Typed in by hand: a negotiated 35.
                    OrderDocument(
                        id=2,
                        tenant_id=ACME,
                        order_id=1,
                        document_type_id=1,
                        source_language="ka",
                        target_language="en",
                        page_count=2,
                        copy_type=CopyType.ORIGINAL,
                        price=Decimal("35.00"),
                        translator_cost=Decimal("20.00"),
                        notary_cost=Decimal("0"),
                    ),
                ]
            )
            await session.commit()
        with tenant_scope(ACME):
            yield session

    await engine.dispose()


async def test_another_bureaus_translator_is_refused(db: AsyncSession) -> None:
    """Order creation stored translator/notary ids unchecked. The FK points at
    the global table, so another bureau's id was accepted — and a made-up one
    500'd, which told the caller which ids exist elsewhere."""
    with pytest.raises(ValidationError, match="translator"):
        await orders._check_assignees(db, _session(), translator_id=2, notary_id=None)
    with pytest.raises(ValidationError, match="notary"):
        await orders._check_assignees(db, _session(), translator_id=None, notary_id=1)
    assert await orders._check_assignees(db, _session(), translator_id=1, notary_id=None)


async def test_a_plan_without_translators_cannot_assign_one(db: AsyncSession) -> None:
    freelancer = _session(
        plan=TenantPlan.FREELANCER,
        permissions=effective_permissions(Role.OWNER, TenantPlan.FREELANCER),
    )
    with pytest.raises(ValidationError, match="plan"):
        await orders._check_assignees(db, freelancer, translator_id=1, notary_id=None)


async def test_an_urgency_change_reprices_only_what_was_not_typed_in(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(orders, "_load_detail", _nothing)
    monkeypatch.setattr("suliko.core.audit.record", _nothing)

    await orders.update_order(1, orders.OrderUpdate(urgency=Urgency.EXPRESS), db, _session())

    docs = {
        d.id: d
        for d in (await db.execute(select(OrderDocument).order_by(OrderDocument.id))).scalars()
    }
    # Computed one follows the multiplier (x1.5)...
    assert docs[1].price == Decimal("60.00")
    assert docs[1].translator_cost == Decimal("30.00")
    # ...the negotiated price stays, while its untouched translator cost moves.
    assert docs[2].price == Decimal("35.00")
    assert docs[2].translator_cost == Decimal("30.00")


async def test_switching_to_courier_adds_the_fee_and_back_removes_it(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(orders, "_load_detail", _nothing)
    monkeypatch.setattr("suliko.core.audit.record", _nothing)

    await orders.update_order(
        1,
        orders.OrderUpdate(handover_method=HandoverMethod.DELIVERY, delivery_address="Rustaveli 1"),
        db,
        _session(),
    )
    order = await db.get(Order, 1)
    assert order is not None and order.delivery_cost == Decimal("10")

    await orders.update_order(
        1, orders.OrderUpdate(handover_method=HandoverMethod.SCAN), db, _session()
    )
    assert order.delivery_cost == Decimal("0")


# ── List extras ─────────────────────────────────────────────────────────────


def test_list_extras_groups_pairs_and_names_once_each() -> None:
    extras = orders.list_extras(
        [
            (1, "ka", "en", "Nino"),
            (1, "ka", "en", "Nino"),
            (1, "en", "ka", None),
            (2, "ru", "ka", "Giorgi"),
        ]
    )
    assert extras == {1: (["Nino"], ["ka→en", "en→ka"]), 2: (["Giorgi"], ["ru→ka"])}


async def test_list_extras_are_loaded_in_one_query(db: AsyncSession) -> None:
    document = await db.get(OrderDocument, 2)
    assert document is not None
    document.translator_id = 1
    await db.flush()

    extras = await orders._load_list_extras(db, [1])
    assert extras == {1: (["Acme translator"], ["ka→en"])}
    assert await orders._load_list_extras(db, []) == {}
