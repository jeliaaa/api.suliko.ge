"""Loading a dump of the old tarjimnebi system.

The dump is hand-made here, in the shape `scripts/tarjimnebi/extract.js`
writes. In-memory SQLite, as in `test_finance_ledgers.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.db.base import Base
from suliko.db.tenancy import install_tenant_filter, tenant_scope
from suliko.domain.tarjimnebi_import import Dump, RefusedError, import_dump, payment_method
from suliko.models.collaboration import OrderComment
from suliko.models.directory import Client, Notary, Translator, TranslatorLanguagePair
from suliko.models.finance import (
    ClientPayment,
    ClientPaymentAllocation,
    Expense,
    NotaryPayment,
    NotaryPaymentAllocation,
    PaymentMethod,
    TranslatorPayment,
    TranslatorPaymentAllocation,
)
from suliko.models.order import Order, OrderDocument, OrderStatusEvent
from suliko.models.reference import DocumentType, Language
from suliko.models.tenant import Tenant, TenantStatus

ACME = 1

TABLES = [
    Tenant.__table__,
    Client.__table__,
    Translator.__table__,
    TranslatorLanguagePair.__table__,
    Notary.__table__,
    Language.__table__,
    DocumentType.__table__,
    Order.__table__,
    OrderDocument.__table__,
    OrderStatusEvent.__table__,
    OrderComment.__table__,
    ClientPayment.__table__,
    ClientPaymentAllocation.__table__,
    TranslatorPayment.__table__,
    TranslatorPaymentAllocation.__table__,
    NotaryPayment.__table__,
    NotaryPaymentAllocation.__table__,
    Expense.__table__,
]


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add(Tenant(id=ACME, slug="acme", display_name="Acme", status=TenantStatus.ACTIVE))
        await session.commit()
        yield session
    await engine.dispose()


def _dump() -> Dump:
    doc: dict[str, Any] = {
        "document_type_id": 6,
        "source_language": "ka",
        "target_language": "en",
        "translator_id": 2,
        "notarized": True,
        "notary_id": 1,
        "copy_type": "plain",
        "page_count": 1,
        "translator_cost": 5.4,
        "notary_cost": 12.08,
        "price": 20,
    }
    second = {**doc, "translator_cost": 4.6, "notary_cost": 7.92, "price": 30}
    return Dump(
        expenses=[
            {
                "date": "2026-10-02",
                "category": "Courier",
                "description": "To the translator",
                "order_id": 1250,
                "amount": 4.5,
            }
        ],
        notaries=[{"id": 1, "name": "N. Notary", "registration_number": "R-1"}],
        translators=[
            {
                "id": 2,
                "name": "T. Translator",
                "phone": "599 11 22 33",
                "experience_from": "2020",
                "default_rate": 15,
                "language_pairs": [{"from": "de", "to": "ka", "price": 15}],
            }
        ],
        clients=[
            {"id": 802, "name": "Client One", "client_type": "B2C", "email": "a@x.ge"},
            {"id": 803, "name": "Client Two", "client_type": "B2B"},
        ],
        orders=[
            {
                "id": 1250,
                "date": "2026-09-30",
                "due_date": "2026-09-30",
                "client_id": 802,
                "handover_methods": ["scan", "pickup"],
                "status": "completed",
                "delivery_cost": 0,
                "documents": [doc, second],
                "payments": [
                    {
                        "payment_id": 865,
                        "date": "2026-09-30",
                        "method": "Bank Transfer",
                        "amount": 50,
                    }
                ],
                "translator_payments": [
                    {
                        "payment_id": 60,
                        "person_id": 2,
                        "date": "2026-10-01",
                        "method": "BOG Bank Transfer",
                        "amount": 10,
                    }
                ],
                "notary_payments": [
                    {
                        "payment_id": 78,
                        "person_id": 1,
                        "date": "2026-10-01",
                        "method": "BOG Bank Transfer",
                        "amount": 20,
                    }
                ],
                "expenses": [],
                "history": [
                    {"status": "completed", "at": "2026-09-30 15:00", "user": "Tako"},
                    {"status": "new", "at": "2026-09-30 10:00", "user": "Tako"},
                ],
                "comments": [
                    {
                        "author_name": "Tako",
                        "message": "hello",
                        "pinned": False,
                        "created_at": "2026-09-30 10:01:00",
                    }
                ],
            },
            {
                "id": 1251,
                "date": "2026-10-01",
                "client_id": 803,
                "status": "new",
                "documents": [doc],
                "payments": [],
                "translator_payments": [],
                "notary_payments": [],
                "expenses": [],
                "history": [],
                "comments": None,
            },
        ],
        reference={
            "document_types": {"6": "ნასამართლობის ცნობა"},
            "languages": {"ka": "ქართული", "en": "ინგლისური", "de": "გერმანული"},
        },
    )


async def _count(db: AsyncSession, model: Any) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


def test_payment_method_names() -> None:
    assert payment_method("Bank Transfer") is PaymentMethod.BANK_TRANSFER
    assert payment_method("BOG Bank Transfer") is PaymentMethod.BANK_TRANSFER
    assert payment_method("BOG Online Payment") is PaymentMethod.BOG_ONLINE
    assert payment_method("Card") is PaymentMethod.CARD
    assert payment_method("Cash") is PaymentMethod.CASH
    assert payment_method("Cheque") is PaymentMethod.OTHER
    assert payment_method(None) is PaymentMethod.OTHER


@pytest.mark.asyncio
async def test_everything_is_loaded_and_ledgers_balance(db: AsyncSession) -> None:
    with tenant_scope(ACME):
        report = await import_dump(db, ACME, _dump(), keep_numbers=True)
        await db.commit()

        assert report.counts["orders created"] == 2
        assert await _count(db, Order) == 2
        assert await _count(db, OrderDocument) == 3
        assert await _count(db, Client) == 2
        assert await _count(db, OrderComment) == 1

        orders = {o.number: o for o in (await db.execute(select(Order))).scalars()}
        assert set(orders) == {1250, 1251}  # the old numbers are kept
        assert orders[1250].source == "tarjimnebi:1250"
        assert orders[1250].handover_method.value == "pickup"

        # Oldest event first, and the order's current status last.
        events = (
            (
                await db.execute(
                    select(OrderStatusEvent)
                    .where(OrderStatusEvent.order_id == orders[1250].id)
                    .order_by(OrderStatusEvent.changed_at)
                )
            )
            .scalars()
            .all()
        )
        assert [e.status for e in events] == ["new", "completed"]
        # 10:00 in Tbilisi (UTC+4) is 06:00 UTC.
        assert events[0].changed_at.hour == 6

        paid = (
            await db.execute(select(func.sum(ClientPaymentAllocation.amount_allocated)))
        ).scalar_one()
        assert Decimal(paid) == Decimal("50.00")
        translator_paid = (
            await db.execute(select(func.sum(TranslatorPaymentAllocation.amount_allocated)))
        ).scalar_one()
        assert Decimal(translator_paid) == Decimal("10.00")
        # 20 spread across two documents whose notary fees are 12.08 and 7.92.
        shares = sorted(
            Decimal(a)
            for a in (await db.execute(select(NotaryPaymentAllocation.amount_allocated))).scalars()
        )
        assert shares == [Decimal("7.92"), Decimal("12.08")]
        assert await _count(db, Expense) == 1
        assert await _count(db, TranslatorLanguagePair) == 1
        assert await _count(db, DocumentType) == 1


@pytest.mark.asyncio
async def test_language_codes_are_stored_lower_case(db: AsyncSession) -> None:
    dump = _dump()
    dump.orders[1]["documents"][0]["source_language"] = "Uz"
    with tenant_scope(ACME):
        await import_dump(db, ACME, dump, keep_numbers=False)
        codes = set((await db.execute(select(OrderDocument.source_language))).scalars())
        assert "Uz" not in codes
        assert "uz" in codes


@pytest.mark.asyncio
async def test_a_second_run_is_refused(db: AsyncSession) -> None:
    with tenant_scope(ACME):
        await import_dump(db, ACME, _dump(), keep_numbers=False)
        await db.commit()
        with pytest.raises(RefusedError):
            await import_dump(db, ACME, _dump(), keep_numbers=False)


@pytest.mark.asyncio
async def test_keep_numbers_needs_an_empty_organisation(db: AsyncSession) -> None:
    with tenant_scope(ACME):
        db.add(Client(name="Already here"))
        await db.commit()
        with pytest.raises(RefusedError):
            await import_dump(db, ACME, _dump(), keep_numbers=True)


@pytest.mark.asyncio
async def test_an_existing_client_with_the_same_email_is_reused(db: AsyncSession) -> None:
    with tenant_scope(ACME):
        db.add(Client(name="Same person", email="A@X.ge"))
        await db.commit()
        report = await import_dump(db, ACME, _dump(), keep_numbers=False)
        assert report.counts["clients matched to an existing one"] == 1
        assert await _count(db, Client) == 2
