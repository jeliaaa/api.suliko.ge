"""Finances — the money screen.

Three ledgers (client payments in, translator payouts out, notary payouts
out), an expense ledger, and the balances derived from them.

## Nothing here is a stored balance

Every figure below is computed from allocations at read time. The PHP does the
same and it is the right call: a payment covers several orders at once, so
"amount paid" on an order is `SUM(allocations)` by definition. A cached column
would be a second source of truth that drifts the first time someone edits an
allocation, and the drift is silent — the number still looks like money.

## Unattributed notary fees

`notary_cost` on a document whose `notary_id` is null is a real cost the bureau
owes someone, but nobody recorded who: the office fronted it, or the translator
paid the notary in cash. It gets its own card rather than being folded into
notary payables, because chasing it is a different job from paying a known
notary (docs/02-PRODUCT-SPEC.md §6.3).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Select, func, or_, select

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._shared import PageMeta
from suliko.core.errors import ConflictError, NotFoundError, ValidationError
from suliko.domain.notifications import notify_everyone
from suliko.domain.orders import (
    document_totals_subquery,
    latest_status_subquery,
    paid_subquery,
)
from suliko.domain.statuses import EXCLUDED_FROM_AGGREGATES
from suliko.models.collaboration import NotificationKind
from suliko.models.directory import Client, Notary, Translator
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
from suliko.models.order import Order, OrderDocument
from suliko.security.permissions import Permission

router = APIRouter(prefix="/finances", tags=["finances"])

ZERO = Decimal("0.00")


def _money(value: Any) -> Decimal:
    """Coerce a possibly-null SQL aggregate to a Decimal.

    `SUM` over no rows is NULL, not 0, and that NULL propagates through every
    subtraction it touches.
    """
    return Decimal(value or 0)


# ── Overview ────────────────────────────────────────────────────────────────


class PeriodCashFlow(BaseModel):
    start: date | None
    end: date | None
    payments_received: Decimal
    translator_payouts: Decimal
    notary_payouts: Decimal
    expenses: Decimal
    #: received minus (payouts + expenses). Negative is a loss-making period.
    net: Decimal


class FinanceOverview(BaseModel):
    """The cards across the top of /finances."""

    #: Owed to us by clients, across orders that are not fully paid.
    receivables: Decimal
    receivable_clients: int
    #: Owed by us to translators for work already delivered.
    payables_translators: Decimal
    #: Owed by us to notaries we can name.
    payables_notaries: Decimal
    #: Notary cost with no notary recorded — see the module docstring.
    unattributed_notary_fees: Decimal
    #: receivables minus (translator + notary payables).
    net_outstanding: Decimal
    period: PeriodCashFlow


def _live_orders() -> Any:
    """Orders that count: everything except cancelled.

    Returned as a subquery of order ids so callers can join it without
    repeating the status join.
    """
    status = latest_status_subquery()
    return (
        select(Order.id.label("order_id"))
        .outerjoin(status, status.c.order_id == Order.id)
        .where(func.coalesce(status.c.status, "").notin_(tuple(EXCLUDED_FROM_AGGREGATES)))
        .subquery("live_orders")
    )


async def _sum_in_period(
    db: Db,
    column: Any,
    date_column: Any,
    start: date | None,
    end: date | None,
) -> Decimal:
    stmt = select(func.coalesce(func.sum(column), 0))
    if start is not None:
        stmt = stmt.where(date_column >= start)
    if end is not None:
        stmt = stmt.where(date_column <= end)
    return _money(await db.scalar(stmt))


@router.get("/overview", response_model=FinanceOverview)
async def overview(
    db: Db,
    _: Annotated[object, Depends(require(Permission.FINANCE_READ))],
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> FinanceOverview:
    # Default period is the current month, matching the screen's initial state.
    if start is None and end is None:
        today = datetime.now(UTC).date()
        start, end = today.replace(day=1), today

    live = _live_orders()
    docs = document_totals_subquery()
    paid = paid_subquery()

    # ── Receivables: billed minus paid, per order, counted only where positive.
    # Per order and not per client, because an overpaid order must not cancel
    # out an unpaid one — the client still owes the second.
    owed = (
        select(
            Order.client_id.label("client_id"),
            (
                func.coalesce(docs.c.documents_total, 0)
                + Order.delivery_cost
                - func.coalesce(paid.c.paid, 0)
            ).label("owed"),
        )
        .join(live, live.c.order_id == Order.id)
        .outerjoin(docs, docs.c.order_id == Order.id)
        .outerjoin(paid, paid.c.order_id == Order.id)
        .subquery()
    )
    receivables_row = (
        await db.execute(
            select(
                func.coalesce(func.sum(owed.c.owed), 0),
                func.count(func.distinct(owed.c.client_id)),
            )
            .select_from(owed)
            .where(owed.c.owed > 0)
        )
    ).one()

    # ── Translator payables: earned on live orders, minus allocated payouts.
    earned_t = await db.scalar(
        select(func.coalesce(func.sum(OrderDocument.translator_cost), 0))
        .select_from(OrderDocument)
        .join(live, live.c.order_id == OrderDocument.order_id)
        .where(OrderDocument.translator_id.is_not(None))
    )
    paid_t = await db.scalar(
        select(func.coalesce(func.sum(TranslatorPaymentAllocation.amount_allocated), 0))
        .select_from(TranslatorPaymentAllocation)
        .join(live, live.c.order_id == TranslatorPaymentAllocation.order_id)
    )

    # ── Notary payables: same shape, but allocations key on the document.
    earned_n = await db.scalar(
        select(func.coalesce(func.sum(OrderDocument.notary_cost), 0))
        .select_from(OrderDocument)
        .join(live, live.c.order_id == OrderDocument.order_id)
        .where(OrderDocument.notary_id.is_not(None))
    )
    paid_n = await db.scalar(
        select(func.coalesce(func.sum(NotaryPaymentAllocation.amount_allocated), 0))
        .select_from(NotaryPaymentAllocation)
        .join(OrderDocument, OrderDocument.id == NotaryPaymentAllocation.order_document_id)
        .join(live, live.c.order_id == OrderDocument.order_id)
        .where(OrderDocument.notary_id.is_not(None))
    )

    unattributed = await db.scalar(
        select(func.coalesce(func.sum(OrderDocument.notary_cost), 0))
        .select_from(OrderDocument)
        .join(live, live.c.order_id == OrderDocument.order_id)
        .where(OrderDocument.notary_id.is_(None), OrderDocument.notary_cost > 0)
    )

    received = await _sum_in_period(
        db, ClientPayment.amount, ClientPayment.payment_date, start, end
    )
    payouts_t = await _sum_in_period(
        db, TranslatorPayment.amount, TranslatorPayment.payment_date, start, end
    )
    payouts_n = await _sum_in_period(
        db, NotaryPayment.amount, NotaryPayment.payment_date, start, end
    )
    period_expenses = await _sum_in_period(db, Expense.amount, Expense.expense_date, start, end)

    receivables = _money(receivables_row[0])
    payables_translators = max(_money(earned_t) - _money(paid_t), ZERO)
    payables_notaries = max(_money(earned_n) - _money(paid_n), ZERO)

    return FinanceOverview(
        receivables=receivables,
        receivable_clients=int(receivables_row[1] or 0),
        payables_translators=payables_translators,
        payables_notaries=payables_notaries,
        unattributed_notary_fees=_money(unattributed),
        net_outstanding=receivables - payables_translators - payables_notaries,
        period=PeriodCashFlow(
            start=start,
            end=end,
            payments_received=received,
            translator_payouts=payouts_t,
            notary_payouts=payouts_n,
            expenses=period_expenses,
            net=received - payouts_t - payouts_n - period_expenses,
        ),
    )


# ── Who owes what ───────────────────────────────────────────────────────────


class ClientBalance(BaseModel):
    client_id: int
    client_name: str
    orders: int
    billed: Decimal
    paid: Decimal
    outstanding: Decimal
    oldest_unpaid: date | None


class TranslatorBalance(BaseModel):
    translator_id: int
    translator_name: str
    earned: Decimal
    paid: Decimal
    outstanding: Decimal


@router.get("/receivables", response_model=list[ClientBalance])
async def receivables(
    db: Db,
    _: Annotated[object, Depends(require(Permission.FINANCE_READ))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ClientBalance]:
    """Clients with an outstanding balance, largest first."""
    live = _live_orders()
    docs = document_totals_subquery()
    paid = paid_subquery()

    per_order = (
        select(
            Order.client_id.label("client_id"),
            Order.order_date.label("order_date"),
            (func.coalesce(docs.c.documents_total, 0) + Order.delivery_cost).label("billed"),
            func.coalesce(paid.c.paid, 0).label("paid"),
            (
                func.coalesce(docs.c.documents_total, 0)
                + Order.delivery_cost
                - func.coalesce(paid.c.paid, 0)
            ).label("owed"),
        )
        .join(live, live.c.order_id == Order.id)
        .outerjoin(docs, docs.c.order_id == Order.id)
        .outerjoin(paid, paid.c.order_id == Order.id)
        .subquery()
    )

    rows = (
        await db.execute(
            select(
                per_order.c.client_id,
                Client.name,
                func.count(),
                func.coalesce(func.sum(per_order.c.billed), 0),
                func.coalesce(func.sum(per_order.c.paid), 0),
                func.coalesce(func.sum(per_order.c.owed), 0),
                func.min(per_order.c.order_date),
            )
            .select_from(per_order)
            .join(Client, Client.id == per_order.c.client_id)
            # Only the unpaid orders are aggregated, so "billed" reads as
            # "billed on what is still outstanding" rather than lifetime spend.
            .where(per_order.c.owed > 0)
            .group_by(per_order.c.client_id, Client.name)
            .order_by(func.sum(per_order.c.owed).desc())
            .limit(limit)
        )
    ).all()

    return [
        ClientBalance(
            client_id=row[0],
            client_name=row[1],
            orders=int(row[2]),
            billed=_money(row[3]),
            paid=_money(row[4]),
            outstanding=_money(row[5]),
            oldest_unpaid=row[6],
        )
        for row in rows
    ]


@router.get("/payables", response_model=list[TranslatorBalance])
async def payables(
    db: Db,
    _: Annotated[object, Depends(require(Permission.FINANCE_READ))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[TranslatorBalance]:
    """Translators owed money, largest first."""
    live = _live_orders()

    earned = (
        select(
            OrderDocument.translator_id.label("translator_id"),
            func.coalesce(func.sum(OrderDocument.translator_cost), 0).label("earned"),
        )
        .join(live, live.c.order_id == OrderDocument.order_id)
        .where(OrderDocument.translator_id.is_not(None))
        .group_by(OrderDocument.translator_id)
        .subquery()
    )

    # A translator payment is allocated to ORDERS, so it is attributed back to
    # the translator through the payment row, not through the allocation.
    settled = (
        select(
            TranslatorPayment.translator_id.label("translator_id"),
            func.coalesce(func.sum(TranslatorPaymentAllocation.amount_allocated), 0).label("paid"),
        )
        .join(
            TranslatorPaymentAllocation,
            TranslatorPaymentAllocation.payment_id == TranslatorPayment.id,
        )
        .group_by(TranslatorPayment.translator_id)
        .subquery()
    )

    rows = (
        await db.execute(
            select(
                earned.c.translator_id,
                Translator.name,
                earned.c.earned,
                func.coalesce(settled.c.paid, 0),
            )
            .select_from(earned)
            .join(Translator, Translator.id == earned.c.translator_id)
            .outerjoin(settled, settled.c.translator_id == earned.c.translator_id)
            .where(earned.c.earned > func.coalesce(settled.c.paid, 0))
            .order_by((earned.c.earned - func.coalesce(settled.c.paid, 0)).desc())
            .limit(limit)
        )
    ).all()

    return [
        TranslatorBalance(
            translator_id=row[0],
            translator_name=row[1],
            earned=_money(row[2]),
            paid=_money(row[3]),
            outstanding=_money(row[2]) - _money(row[3]),
        )
        for row in rows
    ]


# ── Expense ledger ──────────────────────────────────────────────────────────

EXPENSE_CATEGORIES = (
    "courier",
    "notary_office",
    "office",
    "salary",
    "software",
    "tax",
    "other",
)


class ExpenseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expense_date: date
    category: str = Field(max_length=50)
    description: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0, le=Decimal("1000000"))
    #: Null means a general business expense; set it to charge one order.
    order_id: int | None = None

    @model_validator(mode="after")
    def _check_category(self) -> ExpenseIn:
        if self.category not in EXPENSE_CATEGORIES:
            raise ValueError(f"category must be one of: {', '.join(EXPENSE_CATEGORIES)}")
        return self


class ExpenseOut(BaseModel):
    id: int
    expense_date: date
    category: str
    description: str
    amount: Decimal
    order_id: int | None
    recorded_by_user_id: int | None
    created_at: datetime


class ExpensePage(BaseModel):
    items: list[ExpenseOut]
    meta: PageMeta
    #: Total over the whole filtered set, not just this page — the screen
    #: shows "Showing 20 of 340 · ₾12,480.00" and the second figure must not
    #: change as you page.
    total_amount: Decimal


def _expense_filters(
    stmt: Select[Any],
    start: date | None,
    end: date | None,
    category: str | None,
    order_id: int | None,
    search: str | None,
) -> Select[Any]:
    if start is not None:
        stmt = stmt.where(Expense.expense_date >= start)
    if end is not None:
        stmt = stmt.where(Expense.expense_date <= end)
    if category:
        stmt = stmt.where(Expense.category == category)
    if order_id is not None:
        stmt = stmt.where(Expense.order_id == order_id)
    if search:
        stmt = stmt.where(Expense.description.ilike(f"%{search}%"))
    return stmt


@router.get("/expenses", response_model=ExpensePage)
async def list_expenses(
    db: Db,
    _: Annotated[object, Depends(require(Permission.FINANCE_READ))],
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    category: Annotated[str | None, Query(max_length=50)] = None,
    order_id: Annotated[int | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    sort: Literal["date", "-date", "amount", "-amount"] = "-date",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExpensePage:
    stmt = _expense_filters(select(Expense), start, end, category, order_id, search)

    counts = (
        await db.execute(
            _expense_filters(
                select(func.count(), func.coalesce(func.sum(Expense.amount), 0)).select_from(
                    Expense
                ),
                start,
                end,
                category,
                order_id,
                search,
            )
        )
    ).one()

    column = Expense.amount if sort.lstrip("-").startswith("amount") else Expense.expense_date
    stmt = stmt.order_by(column.desc() if sort.startswith("-") else column.asc(), Expense.id.desc())

    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()

    return ExpensePage(
        items=[ExpenseOut.model_validate(r, from_attributes=True) for r in rows],
        meta=PageMeta(total=int(counts[0] or 0), limit=limit, offset=offset),
        total_amount=_money(counts[1]),
    )


@router.post("/expenses", response_model=ExpenseOut, status_code=http_status.HTTP_201_CREATED)
async def create_expense(
    payload: ExpenseIn,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.FINANCE_EXPENSES))],
) -> ExpenseOut:
    # Confirms the order exists AND belongs to this tenant — the ORM filter
    # makes a foreign order simply not found.
    if payload.order_id is not None and await db.get(Order, payload.order_id) is None:
        raise NotFoundError("Order not found.")

    row = Expense(**payload.model_dump(), recorded_by_user_id=session.user_id)
    db.add(row)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="expense.created",
        entity_type="expense",
        entity_id=row.id,
        after=payload.model_dump(mode="json"),
    )
    return ExpenseOut.model_validate(row, from_attributes=True)


@router.delete("/expenses/{expense_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_expense(
    expense_id: int,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.FINANCE_EXPENSES))],
) -> None:
    row = await db.get(Expense, expense_id)
    if row is None:
        raise NotFoundError("Expense not found.")

    from suliko.core.audit import record

    # Recorded before the delete: after it, there is nothing left to describe.
    await record(
        db,
        session,
        action="expense.deleted",
        entity_type="expense",
        entity_id=row.id,
        before={
            "expense_date": row.expense_date.isoformat(),
            "category": row.category,
            "description": row.description,
            "amount": str(row.amount),
            "order_id": row.order_id,
        },
    )
    await db.delete(row)


# ── Client payments ─────────────────────────────────────────────────────────


class Allocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int
    amount_allocated: Decimal = Field(gt=0, le=Decimal("1000000"))


class PaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: int
    amount: Decimal = Field(gt=0, le=Decimal("1000000"))
    payment_date: date
    method: PaymentMethod = PaymentMethod.BANK_TRANSFER
    notes: str | None = Field(default=None, max_length=255)
    #: How the payment is split across orders. One payment, N orders — this is
    #: how a client settling six invoices with one transfer is represented.
    allocations: list[Allocation] = Field(default_factory=list, max_length=200)
    #: Deduplicates a double-submitted form or a retried request.
    idempotency_key: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _allocations_fit(self) -> PaymentIn:
        allocated = sum((a.amount_allocated for a in self.allocations), ZERO)
        if allocated > self.amount:
            raise ValueError(
                f"Allocations total {allocated} but the payment is only {self.amount}."
            )
        seen = {a.order_id for a in self.allocations}
        if len(seen) != len(self.allocations):
            raise ValueError("The same order appears twice in the allocations.")
        return self


class AllocationOut(BaseModel):
    order_id: int
    amount_allocated: Decimal


class PaymentOut(BaseModel):
    id: int
    client_id: int
    client_name: str | None = None
    amount: Decimal
    payment_date: date
    method: PaymentMethod
    notes: str | None
    recorded_by_user_id: int | None
    allocations: list[AllocationOut] = Field(default_factory=list)
    #: amount minus the sum of allocations. Money received but not yet assigned to a job.
    unallocated: Decimal


class PaymentPage(BaseModel):
    items: list[PaymentOut]
    meta: PageMeta
    total_amount: Decimal


async def _payment_out(db: Db, row: ClientPayment) -> PaymentOut:
    allocations = (
        (
            await db.execute(
                select(ClientPaymentAllocation).where(ClientPaymentAllocation.payment_id == row.id)
            )
        )
        .scalars()
        .all()
    )
    allocated = sum((a.amount_allocated for a in allocations), ZERO)
    client = await db.get(Client, row.client_id)

    return PaymentOut(
        id=row.id,
        client_id=row.client_id,
        client_name=client.name if client else None,
        amount=row.amount,
        payment_date=row.payment_date,
        method=row.method,
        notes=row.notes,
        recorded_by_user_id=row.recorded_by_user_id,
        allocations=[
            AllocationOut(order_id=a.order_id, amount_allocated=a.amount_allocated)
            for a in allocations
        ],
        unallocated=row.amount - allocated,
    )


@router.get("/payments", response_model=PaymentPage)
async def list_payments(
    db: Db,
    _: Annotated[object, Depends(require(Permission.FINANCE_READ))],
    client_id: Annotated[int | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    method: Annotated[PaymentMethod | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PaymentPage:
    stmt = select(ClientPayment)
    count_stmt = select(func.count(), func.coalesce(func.sum(ClientPayment.amount), 0)).select_from(
        ClientPayment
    )

    def filtered(statement: Select[Any]) -> Select[Any]:
        if client_id is not None:
            statement = statement.where(ClientPayment.client_id == client_id)
        if start is not None:
            statement = statement.where(ClientPayment.payment_date >= start)
        if end is not None:
            statement = statement.where(ClientPayment.payment_date <= end)
        if method is not None:
            statement = statement.where(ClientPayment.method == method)
        if search:
            pattern = f"%{search}%"
            statement = statement.where(
                or_(
                    ClientPayment.notes.ilike(pattern),
                    ClientPayment.client_id.in_(
                        select(Client.id).where(Client.name.ilike(pattern))
                    ),
                )
            )
        return statement

    counts = (await db.execute(filtered(count_stmt))).one()
    rows = (
        (
            await db.execute(
                filtered(stmt)
                .order_by(ClientPayment.payment_date.desc(), ClientPayment.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    return PaymentPage(
        items=[await _payment_out(db, row) for row in rows],
        meta=PageMeta(total=int(counts[0] or 0), limit=limit, offset=offset),
        total_amount=_money(counts[1]),
    )


@router.post("/payments", response_model=PaymentOut, status_code=http_status.HTTP_201_CREATED)
async def record_payment(
    payload: PaymentIn,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.FINANCE_RECORD_PAYMENT))],
) -> PaymentOut:
    if payload.idempotency_key:
        existing = (
            (
                await db.execute(
                    select(ClientPayment).where(
                        ClientPayment.idempotency_key == payload.idempotency_key
                    )
                )
            )
            .scalars()
            .first()
        )
        # Returns the original rather than 409: a retried request should look
        # like it succeeded, because from the caller's point of view it did.
        if existing is not None:
            return await _payment_out(db, existing)

    if await db.get(Client, payload.client_id) is None:
        raise NotFoundError("Client not found.")

    for allocation in payload.allocations:
        order = await db.get(Order, allocation.order_id)
        if order is None:
            raise NotFoundError(f"Order {allocation.order_id} not found.")
        # Allocating one client's payment to another client's order would
        # quietly move money between accounts.
        if order.client_id != payload.client_id:
            raise ValidationError(f"Order {allocation.order_id} belongs to a different client.")

    payment = ClientPayment(
        client_id=payload.client_id,
        amount=payload.amount,
        payment_date=payload.payment_date,
        method=payload.method,
        notes=payload.notes,
        recorded_by_user_id=session.user_id,
        idempotency_key=payload.idempotency_key,
    )
    db.add(payment)
    await db.flush()

    for allocation in payload.allocations:
        db.add(
            ClientPaymentAllocation(
                payment_id=payment.id,
                order_id=allocation.order_id,
                amount_allocated=allocation.amount_allocated,
            )
        )
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="payment.recorded",
        entity_type="client_payment",
        entity_id=payment.id,
        after=payload.model_dump(mode="json", exclude={"idempotency_key"}),
    )

    client = await db.get(Client, payload.client_id)
    await notify_everyone(
        db,
        kind=NotificationKind.PAYMENT,
        body=f"Payment received - {payload.amount} GEL via {payload.method.value}.",
        actor_user_id=session.user_id,
        actor_name=session.full_name or session.username,
        order_id=payload.allocations[0].order_id if payload.allocations else None,
        subject_label=client.name if client else None,
    )
    await db.flush()

    return await _payment_out(db, payment)


@router.delete("/payments/{payment_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_payment(
    payment_id: int,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.FINANCE_REFUND))],
) -> None:
    """Remove a payment recorded in error.

    Gated on `finance.refund`, not `finance.record_payment`: undoing money is
    a strictly higher-trust action than recording it, and the two are separate
    permissions precisely so a clerk can do the second but not the first.
    """
    row = await db.get(ClientPayment, payment_id)
    if row is None:
        raise NotFoundError("Payment not found.")

    from suliko.models.finance import ClientRefund

    refunded = await db.scalar(
        select(func.count()).select_from(ClientRefund).where(ClientRefund.payment_id == payment_id)
    )
    if refunded:
        raise ConflictError("This payment has refunds recorded against it. Remove those first.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="payment.deleted",
        entity_type="client_payment",
        entity_id=row.id,
        before={
            "client_id": row.client_id,
            "amount": str(row.amount),
            "payment_date": row.payment_date.isoformat(),
            "method": row.method.value,
        },
    )
    # Allocations cascade; the ledger row is the parent.
    await db.delete(row)


# ── Supplier payouts ────────────────────────────────────────────────────────


class PayoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0, le=Decimal("1000000"))
    payment_date: date
    method: PaymentMethod = PaymentMethod.BANK_TRANSFER
    notes: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=64)


class TranslatorPayoutIn(PayoutIn):
    translator_id: int
    #: Which orders this payout settles. Same one-payment-N-allocations shape
    #: as client payments.
    allocations: list[Allocation] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _allocations_fit(self) -> TranslatorPayoutIn:
        allocated = sum((a.amount_allocated for a in self.allocations), ZERO)
        if allocated > self.amount:
            raise ValueError(f"Allocations total {allocated} but the payout is only {self.amount}.")
        return self


class PayoutOut(BaseModel):
    id: int
    amount: Decimal
    payment_date: date
    method: PaymentMethod
    notes: str | None
    unallocated: Decimal


@router.post(
    "/translator-payouts", response_model=PayoutOut, status_code=http_status.HTTP_201_CREATED
)
async def pay_translator(
    payload: TranslatorPayoutIn,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.FINANCE_RECORD_PAYMENT))],
) -> PayoutOut:
    if payload.idempotency_key:
        existing = (
            (
                await db.execute(
                    select(TranslatorPayment).where(
                        TranslatorPayment.idempotency_key == payload.idempotency_key
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            allocated = _money(
                await db.scalar(
                    select(func.coalesce(func.sum(TranslatorPaymentAllocation.amount_allocated), 0))
                    .select_from(TranslatorPaymentAllocation)
                    .where(TranslatorPaymentAllocation.payment_id == existing.id)
                )
            )
            return PayoutOut(
                id=existing.id,
                amount=existing.amount,
                payment_date=existing.payment_date,
                method=existing.method,
                notes=existing.notes,
                unallocated=existing.amount - allocated,
            )

    if await db.get(Translator, payload.translator_id) is None:
        raise NotFoundError("Translator not found.")

    for allocation in payload.allocations:
        if await db.get(Order, allocation.order_id) is None:
            raise NotFoundError(f"Order {allocation.order_id} not found.")

    payout = TranslatorPayment(
        translator_id=payload.translator_id,
        amount=payload.amount,
        payment_date=payload.payment_date,
        method=payload.method,
        notes=payload.notes,
        recorded_by_user_id=session.user_id,
        idempotency_key=payload.idempotency_key,
    )
    db.add(payout)
    await db.flush()

    for allocation in payload.allocations:
        db.add(
            TranslatorPaymentAllocation(
                payment_id=payout.id,
                order_id=allocation.order_id,
                amount_allocated=allocation.amount_allocated,
            )
        )
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="translator_payout.recorded",
        entity_type="translator_payment",
        entity_id=payout.id,
        after=payload.model_dump(mode="json", exclude={"idempotency_key"}),
    )

    allocated = sum((a.amount_allocated for a in payload.allocations), ZERO)
    return PayoutOut(
        id=payout.id,
        amount=payout.amount,
        payment_date=payout.payment_date,
        method=payout.method,
        notes=payout.notes,
        unallocated=payout.amount - allocated,
    )


class NotaryPayoutIn(PayoutIn):
    #: Which documents this settles. Notary cost lives on the document, not
    #: the order, so allocations key on documents. Do not "simplify" this.
    document_ids: list[int] = Field(default_factory=list, max_length=200)
    amounts: list[Decimal] = Field(default_factory=list, max_length=200)
    #: Set when the translator fronted the cash and is reimbursed in the same
    #: transfer as their translation fee.
    paid_via_translator_payment_id: int | None = None

    @model_validator(mode="after")
    def _parallel_lists(self) -> NotaryPayoutIn:
        if len(self.document_ids) != len(self.amounts):
            raise ValueError("document_ids and amounts must be the same length.")
        allocated = sum(self.amounts, ZERO)
        if allocated > self.amount:
            raise ValueError(f"Allocations total {allocated} but the payout is only {self.amount}.")
        return self


@router.post("/notary-payouts", response_model=PayoutOut, status_code=http_status.HTTP_201_CREATED)
async def pay_notary(
    payload: NotaryPayoutIn,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.FINANCE_RECORD_PAYMENT))],
) -> PayoutOut:
    if payload.paid_via_translator_payment_id is not None:
        via = await db.get(TranslatorPayment, payload.paid_via_translator_payment_id)
        if via is None:
            raise NotFoundError("The translator payment it was paid through was not found.")

    for document_id in payload.document_ids:
        if await db.get(OrderDocument, document_id) is None:
            raise NotFoundError(f"Document {document_id} not found.")

    payout = NotaryPayment(
        amount=payload.amount,
        payment_date=payload.payment_date,
        method=payload.method,
        notes=payload.notes,
        paid_via_translator_payment_id=payload.paid_via_translator_payment_id,
        recorded_by_user_id=session.user_id,
        idempotency_key=payload.idempotency_key,
    )
    db.add(payout)
    await db.flush()

    for document_id, amount in zip(payload.document_ids, payload.amounts, strict=True):
        db.add(
            NotaryPaymentAllocation(
                payment_id=payout.id,
                order_document_id=document_id,
                amount_allocated=amount,
            )
        )
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="notary_payout.recorded",
        entity_type="notary_payment",
        entity_id=payout.id,
        after=payload.model_dump(mode="json", exclude={"idempotency_key"}),
    )

    return PayoutOut(
        id=payout.id,
        amount=payout.amount,
        payment_date=payout.payment_date,
        method=payout.method,
        notes=payout.notes,
        unallocated=payout.amount - sum(payload.amounts, ZERO),
    )


# ── Notary balances ─────────────────────────────────────────────────────────


class NotaryBalance(BaseModel):
    notary_id: int
    notary_name: str
    earned: Decimal
    paid: Decimal
    outstanding: Decimal


@router.get("/notary-balances", response_model=list[NotaryBalance])
async def notary_balances(
    db: Db,
    _: Annotated[object, Depends(require(Permission.FINANCE_READ))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NotaryBalance]:
    live = _live_orders()

    earned = (
        select(
            OrderDocument.notary_id.label("notary_id"),
            func.coalesce(func.sum(OrderDocument.notary_cost), 0).label("earned"),
        )
        .join(live, live.c.order_id == OrderDocument.order_id)
        .where(OrderDocument.notary_id.is_not(None))
        .group_by(OrderDocument.notary_id)
        .subquery()
    )

    settled = (
        select(
            OrderDocument.notary_id.label("notary_id"),
            func.coalesce(func.sum(NotaryPaymentAllocation.amount_allocated), 0).label("paid"),
        )
        .join(OrderDocument, OrderDocument.id == NotaryPaymentAllocation.order_document_id)
        .where(OrderDocument.notary_id.is_not(None))
        .group_by(OrderDocument.notary_id)
        .subquery()
    )

    rows = (
        await db.execute(
            select(
                earned.c.notary_id,
                Notary.name,
                earned.c.earned,
                func.coalesce(settled.c.paid, 0),
            )
            .select_from(earned)
            .join(Notary, Notary.id == earned.c.notary_id)
            .outerjoin(settled, settled.c.notary_id == earned.c.notary_id)
            .where(earned.c.earned > func.coalesce(settled.c.paid, 0))
            .order_by((earned.c.earned - func.coalesce(settled.c.paid, 0)).desc())
            .limit(limit)
        )
    ).all()

    return [
        NotaryBalance(
            notary_id=row[0],
            notary_name=row[1],
            earned=_money(row[2]),
            paid=_money(row[3]),
            outstanding=_money(row[2]) - _money(row[3]),
        )
        for row in rows
    ]
