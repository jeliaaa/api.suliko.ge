"""Paying a notary through Bank of Georgia Business Online.

The bank is a fake with the same two outcomes the real one has: it answers
(and may refuse), or it does not answer at all. What these pin is what the
ledger does in each case, because the dangerous failures are quiet ones: a
transfer recorded that was never made, or one made and forgotten.

In-memory SQLite, like `test_finance_ledgers.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.api.v1 import finances, notary_bog
from suliko.core.errors import ConflictError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.plans import TenantPlan, effective_permissions
from suliko.integrations import bog_business
from suliko.integrations.bog_business import (
    BogBusiness,
    BogCreated,
    BogCredentials,
    BogError,
    BogStatus,
    state_of,
)
from suliko.models.directory import Client, ClientType, Notary, Translator
from suliko.models.finance import NotaryPayment, NotaryPaymentAllocation, PaymentMethod
from suliko.models.order import CopyType, Order, OrderDocument, OrderStatusEvent, Urgency
from suliko.models.reference import DocumentType
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role
from suliko.security.sessions import AuthenticatedSession

ACME = 1
TODAY = date(2026, 11, 12)
CREDENTIALS = BogCredentials("id", "secret", "GE00BG0000000000000001", "123456789", "Acme LLC")

TABLES = [
    Tenant.__table__,
    Client.__table__,
    Translator.__table__,
    Notary.__table__,
    DocumentType.__table__,
    Order.__table__,
    OrderDocument.__table__,
    OrderStatusEvent.__table__,
    NotaryPayment.__table__,
    NotaryPaymentAllocation.__table__,
]


# ── The fake bank ───────────────────────────────────────────────────────────


class FakeBank:
    """Stands in for `BogBusiness`. Set `create_error` / `status` to steer it."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.create_error: BogError | None = None
        self.status_report = BogStatus(status="Waiting", reject_code=None, result_code=0)
        self.status_error: BogError | None = None
        self.closed = False

    def __call__(self, _credentials: BogCredentials) -> FakeBank:
        return self

    async def create_domestic(self, **kwargs: Any) -> BogCreated:
        self.created.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return BogCreated(str(kwargs["unique_id"]), 7001, 0, None)

    async def status(self, unique_key: int) -> BogStatus:
        if self.status_error is not None:
            raise self.status_error
        return self.status_report

    async def aclose(self) -> None:
        self.closed = True


def _portable_latest_status() -> Any:
    latest = (
        select(
            OrderStatusEvent.order_id.label("order_id"), func.max(OrderStatusEvent.id).label("m")
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
        .join(latest, latest.c.m == OrderStatusEvent.id)
        .subquery("latest_status")
    )


@pytest.fixture(autouse=True)
def _wired(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _nothing(*_a: Any, **_k: Any) -> int:
        return 0

    async def _credentials(_db: Any, _tenant_id: int) -> BogCredentials:
        return CREDENTIALS

    # DISTINCT ON is PostgreSQL-only; the balance query is not under test here.
    monkeypatch.setattr(finances, "latest_status_subquery", _portable_latest_status)
    monkeypatch.setattr("suliko.core.audit.record", _nothing)
    monkeypatch.setattr(notary_bog, "load_credentials", _credentials)


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


def _session() -> AuthenticatedSession:
    return replace(
        AuthenticatedSession(
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
    )


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """Notary 1 has bank details; notary 2 has none. Documents 11 and 12 are
    notary 1's (20 and 15 owed); document 21 is notary 2's."""
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
                    Translator(id=1, tenant_id=ACME, name="Giorgi"),
                    Notary(
                        id=1,
                        tenant_id=ACME,
                        name="Notary One",
                        bank_iban="GE29 TB 0000000000000001",
                        bank_code="TBCBGE22",
                        bank_inn="01017038380",
                    ),
                    Notary(id=2, tenant_id=ACME, name="Notary Two"),
                    DocumentType(id=1, tenant_id=ACME, name_en="Passport", name_ka="პასპორტი"),
                    Order(
                        id=1,
                        tenant_id=ACME,
                        client_id=1,
                        order_date=TODAY,
                        urgency=Urgency.STANDARD,
                        delivery_cost=Decimal("0"),
                        number=1512,
                    ),
                ]
            )
            for doc_id, notary_id, cost in [(11, 1, "20"), (12, 1, "15"), (21, 2, "10")]:
                session.add(
                    OrderDocument(
                        id=doc_id,
                        tenant_id=ACME,
                        order_id=1,
                        document_type_id=1,
                        source_language="ka",
                        target_language="en",
                        page_count=1,
                        copy_type=CopyType.NOTARY_ORIGINAL,
                        is_notarized=True,
                        price=Decimal("50"),
                        translator_cost=Decimal("0"),
                        notary_cost=Decimal(cost),
                        translator_id=1,
                        notary_id=notary_id,
                    )
                )
            session.add(
                OrderStatusEvent(
                    tenant_id=ACME,
                    order_id=1,
                    status="new",
                    changed_at=datetime(2026, 11, 12, 10, tzinfo=UTC),
                )
            )
            await session.commit()
        with tenant_scope(ACME):
            yield session


def _body(**over: Any) -> notary_bog.BogNotaryPayoutIn:
    fields: dict[str, Any] = {
        "notary_id": 1,
        "document_ids": [11, 12],
        "amounts": [Decimal("20"), Decimal("15")],
        "payment_date": TODAY,
        "idempotency_key": "key-0000001",
    }
    fields.update(over)
    return notary_bog.BogNotaryPayoutIn(**fields)


async def _pay(db: AsyncSession, bank: FakeBank, **over: Any) -> notary_bog.BogPayoutOut:
    return await notary_bog.pay_notary_through_bog(_body(**over), db, _session(), bank)


async def _count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).select_from(NotaryPayment)) or 0


# ── Creating the transfer ───────────────────────────────────────────────────


async def test_a_transfer_is_sent_to_the_bank_and_recorded_pending(db: AsyncSession) -> None:
    bank = FakeBank()
    out = await _pay(db, bank)

    assert (out.state, out.amount, out.unconfirmed) == ("pending", Decimal("35"), False)
    sent = bank.created[0]
    assert sent["amount"] == Decimal("35")
    assert sent["beneficiary_iban"] == "GE29TB0000000000000001"  # spaces stripped
    assert sent["beneficiary_bank_code"] == "TBCBGE22"
    assert sent["beneficiary_inn"] == "01017038380"
    assert "1512" in sent["nomination"] and "35.00" in sent["nomination"]
    assert bank.closed

    row = await db.get(NotaryPayment, out.id)
    assert row is not None
    assert (row.method, row.bog_state, row.bog_unique_key) == (
        PaymentMethod.BOG_ONLINE,
        "pending",
        7001,
    )
    # Pending already counts as paid: the fee is not shown as owed meanwhile.
    owed = await finances.notary_balance(1, db, None)  # type: ignore[arg-type]
    assert owed.outstanding == Decimal("0")


async def test_a_refused_transfer_leaves_nothing_recorded(db: AsyncSession) -> None:
    bank = FakeBank()
    bank.create_error = BogError("The notary's account number is not valid.", reached=True)

    with pytest.raises(ValidationError, match="account number"):
        await _pay(db, bank)

    assert await _count(db) == 0
    owed = await finances.notary_balance(1, db, None)  # type: ignore[arg-type]
    assert owed.outstanding == Decimal("35")


async def test_no_answer_from_the_bank_keeps_the_payment_marked_unconfirmed(
    db: AsyncSession,
) -> None:
    """The bank may have created the document. Forgetting it would invite a
    second transfer; recording it as pending would be a guess. So it is kept
    and said to be unconfirmed."""
    bank = FakeBank()
    bank.create_error = BogError("The bank cannot be reached right now.", reached=False)

    out = await _pay(db, bank)

    assert (out.state, out.unconfirmed) == ("unconfirmed", True)
    assert await _count(db) == 1


async def test_sending_the_same_request_twice_sends_one_transfer(db: AsyncSession) -> None:
    bank = FakeBank()
    first = await _pay(db, bank)
    second = await _pay(db, bank)

    assert second.id == first.id
    assert len(bank.created) == 1
    assert await _count(db) == 1


async def test_a_notary_without_bank_details_is_refused_before_the_bank(
    db: AsyncSession,
) -> None:
    bank = FakeBank()
    with pytest.raises(ValidationError, match="IBAN"):
        await _pay(db, bank, notary_id=2, document_ids=[21], amounts=[Decimal("10")])
    assert bank.created == []


async def test_documents_of_another_notary_are_refused(db: AsyncSession) -> None:
    bank = FakeBank()
    with pytest.raises(ValidationError, match="different notary"):
        await _pay(db, bank, document_ids=[11, 21], amounts=[Decimal("20"), Decimal("10")])
    assert bank.created == []


async def test_more_than_is_owed_is_refused(db: AsyncSession) -> None:
    bank = FakeBank()
    with pytest.raises(ValidationError, match="outstanding"):
        await _pay(db, bank, document_ids=[11], amounts=[Decimal("25")])
    assert bank.created == []


async def test_a_big_transfer_goes_as_an_individual_one() -> None:
    """The bank caps the cheap bulk type at 10 000 lari."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
        seen.extend(__import__("json").loads(request.content))
        return httpx.Response(200, json=[{"UniqueId": "x", "UniqueKey": 5, "ResultCode": 0}])

    bank = BogBusiness(CREDENTIALS, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    for amount, expected in [(Decimal("10000"), "BULK"), (Decimal("10000.01"), "MT103")]:
        seen.clear()
        await bank.create_domestic(
            unique_id=__import__("uuid").uuid4(),
            document_no="N1",
            amount=amount,
            nomination="x",
            value_date=TODAY,
            beneficiary_iban="GE00",
            beneficiary_bank_code="TBCBGE22",
            beneficiary_inn="1",
            beneficiary_name="N",
        )
        assert seen[0]["DispatchType"] == expected
        assert seen[0]["CheckUniqueId"] is True


async def test_what_is_owed_to_a_notary_leaves_out_what_a_transfer_covers(
    db: AsyncSession,
) -> None:
    before = await notary_bog.owed_to_notary(1, db, _session())
    assert [(d.document_id, d.owed) for d in before] == [(11, Decimal("20")), (12, Decimal("15"))]
    assert before[0].order_number == 1512

    await _pay(db, FakeBank(), document_ids=[11], amounts=[Decimal("5")])

    after = await notary_bog.owed_to_notary(1, db, _session())
    assert [(d.document_id, d.owed) for d in after] == [(11, Decimal("15")), (12, Decimal("15"))]


# ── Reading the status back ─────────────────────────────────────────────────


async def test_refresh_completes_a_transfer_the_bank_reports_paid(db: AsyncSession) -> None:
    bank = FakeBank()
    out = await _pay(db, bank)
    bank.status_report = BogStatus(status="Executed", reject_code=None, result_code=0)

    got = await notary_bog.refresh_bog_payout(out.id, db, _session(), bank)

    assert (got.state, got.removed) == ("completed", False)
    row = await db.get(NotaryPayment, out.id)
    assert row is not None and row.bog_state == "completed"


async def test_an_unknown_status_stays_pending(db: AsyncSession) -> None:
    """The bank's docs do not list its statuses. A word that is not clearly
    paid must not mark the fee as paid."""
    bank = FakeBank()
    out = await _pay(db, bank)
    bank.status_report = BogStatus(status="S", reject_code=None, result_code=0)

    got = await notary_bog.refresh_bog_payout(out.id, db, _session(), bank)

    assert (got.state, got.status) == ("pending", "S")


async def test_a_rejected_transfer_is_removed_and_the_fees_are_owed_again(
    db: AsyncSession,
) -> None:
    bank = FakeBank()
    out = await _pay(db, bank)
    bank.status_report = BogStatus(status="Rejected", reject_code=3, result_code=0)

    got = await notary_bog.refresh_bog_payout(out.id, db, _session(), bank)

    assert (got.state, got.removed) == ("rejected", True)
    assert await _count(db) == 0
    owed = await finances.notary_balance(1, db, None)  # type: ignore[arg-type]
    assert owed.outstanding == Decimal("35")


async def test_an_unconfirmed_transfer_cannot_be_refreshed(db: AsyncSession) -> None:
    bank = FakeBank()
    bank.create_error = BogError("down", reached=False)
    out = await _pay(db, bank)

    with pytest.raises(ConflictError, match="never confirmed"):
        await notary_bog.refresh_bog_payout(out.id, db, _session(), bank)


async def test_a_transfer_the_bank_holds_cannot_be_deleted(db: AsyncSession) -> None:
    bank = FakeBank()
    out = await _pay(db, bank)

    with pytest.raises(ConflictError, match="Business Online"):
        await finances.delete_notary_payout(out.id, db, _session())


# ── The bank client and the status words ────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "reject", "expected"),
    [
        ("Executed", None, "completed"),
        ("Completed", None, "completed"),
        ("Rejected", None, "rejected"),
        ("Cancelled by user", None, "rejected"),
        ("anything", 5, "rejected"),
        ("S", None, "pending"),
        ("", None, "pending"),
        (None, None, "pending"),
        ("Waiting for signature", None, "pending"),
    ],
)
def test_status_words(status: str | None, reject: int | None, expected: str) -> None:
    assert state_of(status, reject) == expected


async def test_a_result_code_other_than_ready_to_sign_is_a_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(200, json=[{"UniqueId": "x", "UniqueKey": 0, "ResultCode": 147}])

    bank = BogBusiness(CREDENTIALS, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(BogError, match="IBAN") as caught:
        await bank.create_domestic(
            unique_id=__import__("uuid").uuid4(),
            document_no="N1",
            amount=Decimal("1"),
            nomination="x",
            value_date=TODAY,
            beneficiary_iban="bad",
            beneficiary_bank_code="TBCBGE22",
            beneficiary_inn="1",
            beneficiary_name=None,
        )
    assert caught.value.reached is True


async def test_a_wrong_secret_is_reported_as_such() -> None:
    bank = BogBusiness(
        CREDENTIALS,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))),
    )
    with pytest.raises(BogError, match="Client secret"):
        await bank.check()


async def test_a_dropped_connection_is_not_a_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "token" in request.url.path:
            return httpx.Response(200, json={"access_token": "t"})
        raise httpx.ConnectTimeout("slow")

    bank = BogBusiness(CREDENTIALS, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(BogError) as caught:
        await bank.create_domestic(
            unique_id=__import__("uuid").uuid4(),
            document_no="N1",
            amount=Decimal("1"),
            nomination="x",
            value_date=TODAY,
            beneficiary_iban="GE00",
            beneficiary_bank_code="TBCBGE22",
            beneficiary_inn="1",
            beneficiary_name=None,
        )
    assert caught.value.reached is False


def test_the_module_exposes_what_the_router_imports() -> None:
    assert bog_business.READY_TO_SIGN == 0
    assert datetime.now(UTC)  # keeps the import used if the suite is trimmed
