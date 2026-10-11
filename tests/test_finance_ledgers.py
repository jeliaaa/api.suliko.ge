"""Ledger rules, run against a database.

`test_finance_rules.py` pins what the request models accept. These pin what
the handlers then do with the ledgers: an allocation cannot exceed what an
order owes, a retried payment is recorded once, a payout that is not
allocated still counts, and a notary fee a translator fronted is not counted
as their own fee.

In-memory SQLite, as in `test_platform.py`. The one PostgreSQL-only piece is
`latest_status_subquery` (DISTINCT ON); it is swapped for an equivalent
portable query here, and has its own rendered-SQL coverage elsewhere.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.api.v1 import finances
from suliko.core.errors import ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain import orders as order_queries
from suliko.domain.plans import TenantPlan, effective_permissions
from suliko.models.directory import Client, ClientType, Notary, Translator
from suliko.models.finance import (
    ClientPayment,
    ClientPaymentAllocation,
    ClientRefund,
    Expense,
    NotaryPayment,
    NotaryPaymentAllocation,
    TranslatorPayment,
    TranslatorPaymentAllocation,
)
from suliko.models.order import CopyType, Order, OrderDocument, OrderStatusEvent, Urgency
from suliko.models.reference import DocumentType
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role
from suliko.security.sessions import AuthenticatedSession

ACME = 1
TODAY = date(2026, 9, 20)

TABLES = [
    Tenant.__table__,
    Client.__table__,
    Translator.__table__,
    Notary.__table__,
    DocumentType.__table__,
    Order.__table__,
    OrderDocument.__table__,
    OrderStatusEvent.__table__,
    ClientPayment.__table__,
    ClientPaymentAllocation.__table__,
    ClientRefund.__table__,
    TranslatorPayment.__table__,
    TranslatorPaymentAllocation.__table__,
    NotaryPayment.__table__,
    NotaryPaymentAllocation.__table__,
    Expense.__table__,
]


def _portable_latest_status() -> Any:
    latest = (
        select(
            OrderStatusEvent.order_id.label("order_id"),
            func.max(OrderStatusEvent.id).label("max_id"),
        )
        .group_by(OrderStatusEvent.order_id)
        .subquery("latest_ids")
    )
    return (
        select(
            OrderStatusEvent.order_id.label("order_id"),
            OrderStatusEvent.status.label("status"),
            OrderStatusEvent.changed_at.label("changed_at"),
        )
        .join(latest, latest.c.max_id == OrderStatusEvent.id)
        .subquery("latest_status")
    )


@pytest.fixture(autouse=True)
def _portable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(finances, "latest_status_subquery", _portable_latest_status)
    monkeypatch.setattr(order_queries, "latest_status_subquery", _portable_latest_status)

    async def _nothing(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr("suliko.core.audit.record", _nothing)
    monkeypatch.setattr(finances, "notify_permitted", _nothing)


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


def _session() -> AuthenticatedSession:
    session = AuthenticatedSession(
        session_id=1,
        user_id=1,
        username="owner",
        full_name="Owner",
        email="o@acme.ge",
        role=Role.OWNER,
        tenant_id=ACME,
        tenant_slug="acme",
        tenant_name="Acme",
        plan=TenantPlan.BUREAU,
        onboarding_required=False,
        must_change_password=False,
        has_mfa=False,
        permissions=effective_permissions(Role.OWNER, TenantPlan.BUREAU),
        mfa_satisfied_at=None,
        impersonated_by_user_id=None,
    )
    return replace(session)


def _doc(
    doc_id: int,
    order_id: int,
    *,
    price: str,
    translator_id: int | None = 1,
    translator_cost: str = "0",
    notary_id: int | None = None,
    notary_cost: str = "0",
) -> OrderDocument:
    return OrderDocument(
        id=doc_id,
        tenant_id=ACME,
        order_id=order_id,
        document_type_id=1,
        source_language="ka",
        target_language="en",
        page_count=1,
        copy_type=CopyType.NOTARY_ORIGINAL if notary_id else CopyType.ORIGINAL,
        is_notarized=notary_id is not None,
        price=Decimal(price),
        translator_cost=Decimal(translator_cost),
        notary_cost=Decimal(notary_cost),
        translator_id=translator_id,
        notary_id=notary_id,
    )


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """Order 1: owes 100, translator 1 earned 40, notary 1 is owed 20.
    Order 2: cancelled. Order 3: another translator's job."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        with bypass_tenant_scope():
            session.add(
                Tenant(
                    id=ACME,
                    slug="acme",
                    display_name="Acme",
                    status=TenantStatus.ACTIVE,
                    plan="bureau",
                    locale="ka",
                )
            )
            session.add_all(
                [
                    Client(id=1, tenant_id=ACME, name="Nino", client_type=ClientType.B2C),
                    Client(id=2, tenant_id=ACME, name="Other", client_type=ClientType.B2C),
                    Translator(id=1, tenant_id=ACME, name="Giorgi"),
                    Translator(id=2, tenant_id=ACME, name="Ana"),
                    Notary(id=1, tenant_id=ACME, name="Notary Office"),
                    DocumentType(id=1, tenant_id=ACME, name_en="Passport", name_ka="პასპორტი"),
                ]
            )
            for order_id, client_id in [(1, 1), (2, 1), (3, 1)]:
                session.add(
                    Order(
                        id=order_id,
                        tenant_id=ACME,
                        client_id=client_id,
                        order_date=TODAY,
                        urgency=Urgency.STANDARD,
                        delivery_cost=Decimal("0"),
                    )
                )
            session.add_all(
                [
                    _doc(
                        11,
                        1,
                        price="100",
                        translator_cost="40",
                        notary_id=1,
                        notary_cost="20",
                    ),
                    _doc(21, 2, price="50", translator_cost="25"),
                    _doc(31, 3, price="80", translator_id=2, translator_cost="30"),
                ]
            )
            now = datetime(2026, 9, 20, 10, tzinfo=UTC)
            session.add_all(
                [
                    OrderStatusEvent(tenant_id=ACME, order_id=1, status="new", changed_at=now),
                    OrderStatusEvent(tenant_id=ACME, order_id=2, status="new", changed_at=now),
                    OrderStatusEvent(
                        tenant_id=ACME, order_id=2, status="cancelled", changed_at=now
                    ),
                    OrderStatusEvent(tenant_id=ACME, order_id=3, status="new", changed_at=now),
                ]
            )
            await session.commit()
        with tenant_scope(ACME):
            yield session

    await engine.dispose()


def _payment(**overrides: Any) -> finances.PaymentIn:
    fields: dict[str, Any] = {
        "client_id": 1,
        "amount": Decimal("100"),
        "payment_date": TODAY,
        "allocations": [],
    }
    fields.update(overrides)
    return finances.PaymentIn(**fields)


# ── Client payments ─────────────────────────────────────────────────────────


async def test_an_allocation_cannot_exceed_what_the_order_owes(db: AsyncSession) -> None:
    """Otherwise the surplus vanishes into a negative order balance that every
    "who owes" query filters out as `owed > 0`."""
    with pytest.raises(ValidationError, match="only 100"):
        await finances.record_payment(
            _payment(
                amount=Decimal("150"),
                allocations=[finances.Allocation(order_id=1, amount_allocated=Decimal("150"))],
            ),
            db,
            _session(),
        )


async def test_a_cancelled_order_cannot_be_allocated_to(db: AsyncSession) -> None:
    with pytest.raises(ValidationError, match="cancelled or rejected"):
        await finances.record_payment(
            _payment(
                allocations=[finances.Allocation(order_id=2, amount_allocated=Decimal("10"))]
            ),
            db,
            _session(),
        )


async def test_a_retried_payment_is_recorded_once(db: AsyncSession) -> None:
    """The form now keeps one key per form, so a double submit carries the
    same key twice — and must produce one ledger row."""
    first = await finances.record_payment(_payment(idempotency_key="k-1"), db, _session())
    second = await finances.record_payment(_payment(idempotency_key="k-1"), db, _session())

    assert first.id == second.id
    count = await db.scalar(select(func.count()).select_from(ClientPayment))
    assert count == 1


async def test_a_client_account_includes_unallocated_credit(db: AsyncSession) -> None:
    await finances.record_payment(
        _payment(
            amount=Decimal("130"),
            allocations=[finances.Allocation(order_id=1, amount_allocated=Decimal("100"))],
        ),
        db,
        _session(),
    )
    account = await finances.client_account(1, db, None)  # type: ignore[arg-type]

    # Orders 1 and 3 count (2 is cancelled): billed 180, paid 100, 80 owed.
    assert account.orders == 2
    assert account.billed == Decimal("180")
    assert account.outstanding == Decimal("80")
    assert account.unallocated_credit == Decimal("30")


# ── Translator payouts ──────────────────────────────────────────────────────


def _payout(**overrides: Any) -> finances.TranslatorPayoutIn:
    fields: dict[str, Any] = {
        "translator_id": 1,
        "amount": Decimal("40"),
        "payment_date": TODAY,
        "allocations": [],
    }
    fields.update(overrides)
    return finances.TranslatorPayoutIn(**fields)


async def test_a_payout_cannot_be_allocated_to_someone_elses_job(db: AsyncSession) -> None:
    with pytest.raises(ValidationError, match="no documents by this translator"):
        await finances.pay_translator(
            _payout(allocations=[finances.Allocation(order_id=3, amount_allocated=Decimal("10"))]),
            db,
            _session(),
        )


async def test_an_unallocated_payout_still_reduces_what_is_owed(db: AsyncSession) -> None:
    """It used to count only allocations, so a plain transfer left the
    translator showing as owed in full — and paid twice."""
    before = await finances.translator_balance(1, db, None)  # type: ignore[arg-type]
    assert before.outstanding == Decimal("40")  # order 2 is cancelled: not earned

    await finances.pay_translator(_payout(amount=Decimal("25")), db, _session())

    after = await finances.translator_balance(1, db, None)  # type: ignore[arg-type]
    assert after.paid == Decimal("25")
    assert after.outstanding == Decimal("15")


async def test_a_fully_paid_translator_still_has_a_balance(db: AsyncSession) -> None:
    """The detail screen read the "who is owed" list, so a paid-up translator
    showed as having earned and been paid nothing."""
    await finances.pay_translator(_payout(), db, _session())
    balance = await finances.translator_balance(1, db, None)  # type: ignore[arg-type]
    assert (balance.earned, balance.paid, balance.outstanding) == (
        Decimal("40"),
        Decimal("40"),
        Decimal("0"),
    )
    owed = await finances.payables(db, None, 50)  # type: ignore[arg-type]
    assert [b.translator_id for b in owed] == [2]  # only Ana is still owed


async def test_a_fronted_notary_fee_is_not_counted_as_the_translators(db: AsyncSession) -> None:
    """One transfer of 60 = their 40 fee + 20 they paid the notary in cash.
    The 20 is also recorded as a notary payment, so it must not count as
    translator pay too."""
    payout = await finances.pay_translator(_payout(amount=Decimal("60")), db, _session())
    await finances.pay_notary(
        finances.NotaryPayoutIn(
            amount=Decimal("20"),
            payment_date=TODAY,
            document_ids=[11],
            amounts=[Decimal("20")],
            paid_via_translator_payment_id=payout.id,
        ),
        db,
        _session(),
    )

    translator = await finances.translator_balance(1, db, None)  # type: ignore[arg-type]
    notary = await finances.notary_balance(1, db, None)  # type: ignore[arg-type]
    assert translator.paid == Decimal("40")
    assert translator.outstanding == Decimal("0")
    assert notary.outstanding == Decimal("0")

    overview = await finances.overview(db, _session(), TODAY, TODAY)
    # Cash out is 60, not 80: the notary share is inside the translator payout.
    assert overview.period.translator_payouts == Decimal("60")
    assert overview.period.notary_payouts == Decimal("0")


async def test_a_notary_payout_is_idempotent_too(db: AsyncSession) -> None:
    """The one ledger that stored its key and never looked it up."""
    body = finances.NotaryPayoutIn(
        amount=Decimal("5"),
        payment_date=TODAY,
        document_ids=[11],
        amounts=[Decimal("5")],
        idempotency_key="n-1",
    )
    first = await finances.pay_notary(body, db, _session())
    second = await finances.pay_notary(body, db, _session())
    assert first.id == second.id
    assert await db.scalar(select(func.count()).select_from(NotaryPayment)) == 1


async def test_a_notary_allocation_is_capped_at_the_fee(db: AsyncSession) -> None:
    with pytest.raises(ValidationError, match="20"):
        await finances.pay_notary(
            finances.NotaryPayoutIn(
                amount=Decimal("50"),
                payment_date=TODAY,
                document_ids=[11],
                amounts=[Decimal("50")],
            ),
            db,
            _session(),
        )


def test_amounts_are_limited_to_what_the_column_holds() -> None:
    """0.004 passed `gt=0`, was stored as 0.00, and then failed the database's
    `amount > 0` CHECK as a 500."""
    from pydantic import ValidationError as PydanticError

    with pytest.raises(PydanticError):
        _payment(amount=Decimal("0.004"))
    with pytest.raises(PydanticError):
        _payment(amount=Decimal("123456789.00"))
