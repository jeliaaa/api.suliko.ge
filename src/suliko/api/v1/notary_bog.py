"""Paying a notary through Bank of Georgia Business Online.

Office prepares the transfer; the owner signs it in the bank's app. Until that
signature the money has not moved, so a payment here has a state:

- ``pending``: created in the bank, waiting for the signature;
- ``completed``: the bank reports it paid;
- ``unconfirmed``: the bank did not answer, so a document may or may not exist.

A transfer the bank REFUSES is never kept: nothing was sent, so nothing is
recorded and the documents stay owed. One the owner REJECTS in the app is
removed when its status is next read, for the same reason.

The ledger counts a pending transfer as paid at once, as the old system did,
so the notary's balance does not show the fee as owed while the signature is
awaited. Removing it again is what the status refresh is for.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, select

from suliko.api.deps import CurrentSession, Db
from suliko.api.v1._shared import PositiveMoney
from suliko.api.v1.finances import (
    ZERO,
    NotaryPayoutIn,
    PaymentRecorder,
    _check_notary_allocations,
    _insert_once,
    _live_orders,
    _money,
)
from suliko.api.v1.integrations import _stored_secrets
from suliko.core.errors import ConflictError, NotFoundError, ValidationError
from suliko.integrations.bog_business import (
    BogBusiness,
    BogCredentials,
    BogError,
    state_of,
)
from suliko.models.directory import Notary
from suliko.models.finance import NotaryPayment, NotaryPaymentAllocation, PaymentMethod
from suliko.models.integration import IntegrationCredential, IntegrationProvider
from suliko.models.order import Order, OrderDocument
from suliko.models.reference import DocumentType

router = APIRouter(prefix="/finances", tags=["finances"])

BogFactory = Callable[[BogCredentials], BogBusiness]


def get_bog_factory() -> BogFactory:
    """FastAPI dependency. Tests replace it with a fake bank."""
    return BogBusiness


# ── Credentials ─────────────────────────────────────────────────────────────


async def load_credentials(db: Db, tenant_id: int) -> BogCredentials:
    """The organisation's saved bank login, or a plain reason there is none."""
    row = (
        (
            await db.execute(
                select(IntegrationCredential).where(
                    IntegrationCredential.provider == IntegrationProvider.BOG_BUSINESS
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None or not row.is_enabled:
        raise ValidationError(
            "Bank of Georgia transfers are not switched on. Turn on "
            "“Bank of Georgia: Business Online” in Settings, Integrations."
        )
    secrets = _stored_secrets(row, tenant_id)
    values = {
        "client_id": str(row.config.get("client_id", "")).strip(),
        "client_secret": str(secrets.get("client_secret", "")).strip(),
        "source_iban": str(row.config.get("source_iban", "")).replace(" ", "").strip(),
        "payer_inn": str(row.config.get("payer_inn", "")).strip(),
        "payer_name": str(row.config.get("payer_name", "")).strip(),
    }
    if missing := [k for k, v in values.items() if not v]:
        raise ValidationError(
            "The Bank of Georgia settings are incomplete: " + ", ".join(sorted(missing)) + "."
        )
    return BogCredentials(**values)


class BogReadiness(BaseModel):
    ready: bool
    #: Why not, when not ready. Written for the person at the payout screen.
    reason: str | None = None


@router.get("/bog/ready", response_model=BogReadiness)
async def bog_ready(db: Db, session: PaymentRecorder) -> BogReadiness:
    """Whether the payout screen may offer the bank transfer at all."""
    try:
        await load_credentials(db, session.tenant_id)
    except ValidationError as exc:
        return BogReadiness(ready=False, reason=str(exc))
    return BogReadiness(ready=True)


class OwedDocument(BaseModel):
    document_id: int
    order_id: int
    order_number: int | None
    document_type_en: str | None
    document_type_ka: str | None
    #: What is still unpaid of this document's notary fee.
    owed: Decimal


@router.get("/notary-payouts/bog/owed", response_model=list[OwedDocument])
async def owed_to_notary(notary_id: int, db: Db, session: PaymentRecorder) -> list[OwedDocument]:
    """The notary's documents with something still unpaid, oldest first.

    What the payout screen lists to tick. A fee that a pending transfer already
    covers counts as paid, so it is not offered a second time.
    """
    paid = (
        select(
            NotaryPaymentAllocation.order_document_id.label("document_id"),
            func.sum(NotaryPaymentAllocation.amount_allocated).label("paid"),
        )
        .group_by(NotaryPaymentAllocation.order_document_id)
        .subquery("doc_paid")
    )
    live = _live_orders()
    owed = OrderDocument.notary_cost - func.coalesce(paid.c.paid, 0)
    rows = (
        await db.execute(
            select(
                OrderDocument.id,
                OrderDocument.order_id,
                Order.number,
                DocumentType.name_en,
                DocumentType.name_ka,
                owed.label("owed"),
            )
            .join(live, live.c.order_id == OrderDocument.order_id)
            .join(Order, Order.id == OrderDocument.order_id)
            .outerjoin(DocumentType, DocumentType.id == OrderDocument.document_type_id)
            .outerjoin(paid, paid.c.document_id == OrderDocument.id)
            .where(
                OrderDocument.notary_id == notary_id,
                OrderDocument.is_notarized.is_(True),
                owed > 0,
            )
            .order_by(Order.order_date, OrderDocument.id)
            .limit(500)
        )
    ).all()
    return [
        OwedDocument(
            document_id=r[0],
            order_id=r[1],
            order_number=r[2],
            document_type_en=r[3],
            document_type_ka=r[4],
            owed=_money(r[5]),
        )
        for r in rows
    ]


# ── Starting a transfer ─────────────────────────────────────────────────────


class BogNotaryPayoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notary_id: int
    document_ids: list[int] = Field(min_length=1, max_length=200)
    amounts: list[PositiveMoney] = Field(min_length=1, max_length=200)
    payment_date: date
    #: What the bank shows as the payment's purpose. Built from the order
    #: numbers when left out.
    purpose: str | None = Field(default=None, max_length=250)
    notes: str | None = Field(default=None, max_length=255)
    #: Required here (optional on the manual payout): it is what stops a
    #: double click from sending the notary the money twice.
    idempotency_key: str = Field(min_length=8, max_length=64)

    @model_validator(mode="after")
    def _parallel(self) -> BogNotaryPayoutIn:
        if len(self.document_ids) != len(self.amounts):
            raise ValueError("document_ids and amounts must be the same length.")
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("The same document appears twice.")
        return self


class BogPayoutOut(BaseModel):
    id: int
    amount: Decimal
    state: str
    status: str | None
    #: True when the bank gave no answer: nothing is certain, so the screen
    #: says so rather than showing a pending transfer.
    unconfirmed: bool
    purpose: str


async def _default_purpose(db: Db, document_ids: list[int], total: Decimal) -> str:
    numbers = sorted(
        {
            n
            for n in (
                await db.execute(
                    select(Order.number)
                    .join(OrderDocument, OrderDocument.order_id == Order.id)
                    .where(OrderDocument.id.in_(document_ids))
                )
            )
            .scalars()
            .all()
            if n is not None
        }
    )
    # Every order in full: the bank rejects a purpose cut short with "...".
    base = "სანოტარო მომსახურება"
    if not numbers:
        return f"{base} (₾{total:.2f})"
    label = "შეკვეთა" if len(numbers) == 1 else "შეკვეთები"
    orders = ", ".join(f"#{n}" for n in numbers)
    return f"{base}, {label} {orders} (₾{total:.2f})"


@router.post(
    "/notary-payouts/bog",
    response_model=BogPayoutOut,
    status_code=http_status.HTTP_201_CREATED,
)
async def pay_notary_through_bog(
    payload: BogNotaryPayoutIn,
    db: Db,
    session: PaymentRecorder,
    factory: Annotated[BogFactory, Depends(get_bog_factory)],
) -> BogPayoutOut:
    existing = (
        (
            await db.execute(
                select(NotaryPayment).where(
                    NotaryPayment.idempotency_key == payload.idempotency_key
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        # The same request again: report the first one, send nothing.
        return _out(existing, existing.notes or "")

    credentials = await load_credentials(db, session.tenant_id)

    notary = await db.get(Notary, payload.notary_id)
    if notary is None:
        raise NotFoundError("Notary not found.")
    missing = [
        label
        for label, value in (
            ("IBAN", notary.bank_iban),
            ("bank code", notary.bank_code),
            ("tax ID", notary.bank_inn),
        )
        if not (value or "").strip()
    ]
    if missing:
        raise ValidationError(
            f"{notary.name} has no {', '.join(missing)} on file. Add it on the notary's page."
        )

    wrong = (
        (
            await db.execute(
                select(OrderDocument.id).where(
                    OrderDocument.id.in_(payload.document_ids),
                    OrderDocument.notary_id != payload.notary_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if wrong:
        raise ValidationError("Some of these documents belong to a different notary.")

    total = sum(payload.amounts, ZERO)
    manual = NotaryPayoutIn(
        amount=total,
        payment_date=payload.payment_date,
        method=PaymentMethod.BOG_ONLINE,
        notes=payload.notes,
        idempotency_key=payload.idempotency_key,
        document_ids=payload.document_ids,
        amounts=payload.amounts,
    )
    await _check_notary_allocations(db, manual)

    purpose = (payload.purpose or "").strip() or await _default_purpose(
        db, payload.document_ids, total
    )

    unique_id = uuid.uuid4()
    payout = NotaryPayment(
        amount=total,
        payment_date=payload.payment_date,
        method=PaymentMethod.BOG_ONLINE,
        # The purpose is kept in the notes when none was written, so the record
        # says what the bank was told.
        notes=payload.notes or purpose[:255],
        recorded_by_user_id=session.user_id,
        idempotency_key=payload.idempotency_key,
        bog_unique_id=str(unique_id),
    )
    again = await _insert_once(db, payout, NotaryPayment, payload.idempotency_key)
    if again is not None:
        return _out(again, purpose)
    for document_id, amount in zip(payload.document_ids, payload.amounts, strict=True):
        db.add(
            NotaryPaymentAllocation(
                payment_id=payout.id, order_document_id=document_id, amount_allocated=amount
            )
        )
    await db.flush()

    bank = factory(credentials)
    try:
        created = await bank.create_domestic(
            unique_id=unique_id,
            document_no=f"N{payout.id}",
            amount=total,
            nomination=purpose,
            value_date=payload.payment_date,
            beneficiary_iban=(notary.bank_iban or "").replace(" ", ""),
            beneficiary_bank_code=(notary.bank_code or "").strip(),
            beneficiary_inn=(notary.bank_inn or "").strip(),
            beneficiary_name=notary.name,
        )
    except BogError as exc:
        if exc.reached:
            # The bank said no: nothing exists there, so nothing is kept. The
            # request's rollback would do it too; removing the rows here does
            # not depend on that.
            await db.execute(
                delete(NotaryPaymentAllocation).where(
                    NotaryPaymentAllocation.payment_id == payout.id
                )
            )
            await db.delete(payout)
            await db.flush()
            raise ValidationError(exc.message) from exc
        # No answer. A document may exist, so the payment is kept and marked,
        # rather than forgotten and sent again.
        payout.bog_state = "unconfirmed"
        payout.bog_checked_at = datetime.now(UTC)
        await db.flush()
        await _audit(db, session, payout, "notary_payout.bog_unconfirmed", exc.message)
        return _out(payout, purpose)
    finally:
        await bank.aclose()

    payout.bog_unique_key = created.unique_key
    payout.bog_state = "pending"
    payout.bog_status = "Waiting for signature"
    payout.bog_checked_at = datetime.now(UTC)
    await db.flush()
    await _audit(db, session, payout, "notary_payout.bog_created", None)
    return _out(payout, purpose)


def _out(row: NotaryPayment, purpose: str) -> BogPayoutOut:
    return BogPayoutOut(
        id=row.id,
        amount=row.amount,
        state=row.bog_state or "pending",
        status=row.bog_status,
        unconfirmed=row.bog_state == "unconfirmed",
        purpose=purpose,
    )


async def _audit(
    db: Db, session: CurrentSession, row: NotaryPayment, action: str, detail: str | None
) -> None:
    from suliko.core.audit import record

    await record(
        db,
        session,
        action=action,
        entity_type="notary_payment",
        entity_id=row.id,
        after={
            "amount": str(row.amount),
            "bog_unique_key": row.bog_unique_key,
            "bog_state": row.bog_state,
            "bog_status": row.bog_status,
            "detail": detail,
        },
    )


# ── Reading it back ─────────────────────────────────────────────────────────


class BogRefreshOut(BaseModel):
    id: int
    state: str
    status: str | None
    #: True when the owner rejected it in the bank: the record was removed and
    #: the fees are owed again.
    removed: bool


@router.post("/notary-payouts/{payout_id}/bog-refresh", response_model=BogRefreshOut)
async def refresh_bog_payout(
    payout_id: int,
    db: Db,
    session: PaymentRecorder,
    factory: Annotated[BogFactory, Depends(get_bog_factory)],
) -> BogRefreshOut:
    row = await db.get(NotaryPayment, payout_id)
    if row is None or row.method != PaymentMethod.BOG_ONLINE:
        raise NotFoundError("Bank transfer not found.")
    if row.bog_unique_key is None:
        raise ConflictError(
            "The bank never confirmed this transfer. Look for it in Business Online; if it is "
            "not there, remove this record and pay again."
        )
    if row.bog_state in ("completed",):
        return BogRefreshOut(id=row.id, state="completed", status=row.bog_status, removed=False)

    credentials = await load_credentials(db, session.tenant_id)
    bank = factory(credentials)
    try:
        report = await bank.status(row.bog_unique_key)
    except BogError as exc:
        raise ConflictError(exc.message) from exc
    finally:
        await bank.aclose()

    state = state_of(report.status, report.reject_code)
    row.bog_checked_at = datetime.now(UTC)

    if state == "rejected":
        await _audit(
            db, session, row, "notary_payout.bog_rejected", report.status or str(report.reject_code)
        )
        await db.execute(
            delete(NotaryPaymentAllocation).where(NotaryPaymentAllocation.payment_id == row.id)
        )
        await db.delete(row)
        await db.flush()
        return BogRefreshOut(id=payout_id, state="rejected", status=report.status, removed=True)

    row.bog_state = state
    row.bog_status = (report.status or row.bog_status or "")[:60] or None
    await db.flush()
    if state == "completed":
        await _audit(db, session, row, "notary_payout.bog_completed", report.status)
    return BogRefreshOut(id=row.id, state=state, status=row.bog_status, removed=False)


@router.get("/notary-payouts/bog/pending", response_model=list[int])
async def pending_bog_payouts(db: Db, session: PaymentRecorder) -> list[int]:
    """Ids of transfers still waiting for a signature, for the screen to refresh."""
    return list(
        (
            await db.execute(
                select(NotaryPayment.id)
                .where(NotaryPayment.bog_state == "pending")
                .order_by(NotaryPayment.id.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
