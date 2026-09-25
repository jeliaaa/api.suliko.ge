"""Finances — the money screen.

Three ledgers (client payments in, translator payouts out, notary payouts
out), an expense ledger, and the balances derived from them.

## Nothing here is a stored balance

Every figure below is computed from the ledgers at read time. The PHP does the
same and it is the right call: a payment covers several orders at once, so
"amount paid" on an order is `SUM(allocations)` by definition. A cached column
would be a second source of truth that drifts the first time someone edits an
allocation, and the drift is silent — the number still looks like money.

## One definition of "owed", everywhere

The overview cards, the "who is owed" lists and a single person's balance all
come from the same per-person query (`_translator_balances`,
`_notary_balances`), so the card and the table under it cannot disagree:

- **earned** — costs on orders that still count (not cancelled or rejected);
- **paid** — money that actually went to them. For a translator that is every
  payout, allocated or not (as the PHP counts it), minus any part of a payout
  that reimbursed a notary fee they fronted — that part is recorded as a
  notary payment and belongs to the notary's balance, not theirs. For a notary
  it is what was allocated to their documents.

Paid is NOT filtered to live orders: money that went out for a job later
cancelled still went out, and shows as credit rather than vanishing.

## Unattributed notary fees

`notary_cost` on a document whose `notary_id` is null is a real cost the bureau
owes someone, but nobody recorded who: the office fronted it, or the translator
paid the notary in cash. It gets its own card rather than being folded into
notary payables, because chasing it is a different job from paying a known
notary.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Select, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._shared import LIKE_ESCAPE, PageMeta, PositiveMoney, like_pattern
from suliko.core.errors import ConflictError, NotFoundError, ValidationError
from suliko.domain.clock import today_in
from suliko.domain.notifications import notify_permitted
from suliko.domain.orders import (
    document_totals_subquery,
    latest_status_subquery,
    paid_subquery,
)
from suliko.domain.statuses import EXCLUDED_FROM_AGGREGATES, sql_values
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

FinanceReader = Annotated[CurrentSession, Depends(require(Permission.FINANCE_READ))]
PaymentRecorder = Annotated[CurrentSession, Depends(require(Permission.FINANCE_RECORD_PAYMENT))]
#: Undoing money is a strictly higher-trust action than recording it, which is
#: why deletes need `finance.refund` and not `finance.record_payment`.
MoneyUndoer = Annotated[CurrentSession, Depends(require(Permission.FINANCE_REFUND))]


def _money(value: Any) -> Decimal:
    """Coerce a possibly-null SQL aggregate to a Decimal.

    `SUM` over no rows is NULL, not 0, and that NULL propagates through every
    subtraction it touches.
    """
    return Decimal(value or 0)


def _live_orders() -> Any:
    """Orders that count: everything except cancelled and rejected.

    Returned as a subquery of order ids so callers can join it without
    repeating the status join.
    """
    status = latest_status_subquery()
    return (
        select(Order.id.label("order_id"))
        .outerjoin(status, status.c.order_id == Order.id)
        .where(func.coalesce(status.c.status, "").notin_(sql_values(EXCLUDED_FROM_AGGREGATES)))
        .subquery("live_orders")
    )


# ── Balances: the one definition ────────────────────────────────────────────


def _translator_balances() -> Any:
    """Per translator: earned on live orders, and paid (see module docstring)."""
    live = _live_orders()
    earned = (
        select(
            OrderDocument.translator_id.label("translator_id"),
            func.coalesce(func.sum(OrderDocument.translator_cost), 0).label("earned"),
        )
        .join(live, live.c.order_id == OrderDocument.order_id)
        .where(OrderDocument.translator_id.is_not(None))
        .group_by(OrderDocument.translator_id)
        .subquery("t_earned")
    )
    payouts = (
        select(
            TranslatorPayment.translator_id.label("translator_id"),
            func.coalesce(func.sum(TranslatorPayment.amount), 0).label("paid"),
        )
        .group_by(TranslatorPayment.translator_id)
        .subquery("t_payouts")
    )
    # The share of a translator's payouts that reimbursed a notary fee they
    # fronted. Recorded as a notary payment too, so it is taken out here or
    # it would count as both their fee and the notary's.
    fronted = (
        select(
            TranslatorPayment.translator_id.label("translator_id"),
            func.coalesce(func.sum(NotaryPayment.amount), 0).label("fronted"),
        )
        .join(NotaryPayment, NotaryPayment.paid_via_translator_payment_id == TranslatorPayment.id)
        .group_by(TranslatorPayment.translator_id)
        .subquery("t_fronted")
    )
    paid = func.coalesce(payouts.c.paid, 0) - func.coalesce(fronted.c.fronted, 0)
    earned_value = func.coalesce(earned.c.earned, 0)
    return (
        select(
            Translator.id.label("translator_id"),
            Translator.name.label("name"),
            earned_value.label("earned"),
            paid.label("paid"),
            (earned_value - paid).label("outstanding"),
        )
        .outerjoin(earned, earned.c.translator_id == Translator.id)
        .outerjoin(payouts, payouts.c.translator_id == Translator.id)
        .outerjoin(fronted, fronted.c.translator_id == Translator.id)
        .subquery("translator_balances")
    )


def _notary_balances() -> Any:
    """Per notary: earned on live orders, paid by allocation to their documents."""
    live = _live_orders()
    earned = (
        select(
            OrderDocument.notary_id.label("notary_id"),
            func.coalesce(func.sum(OrderDocument.notary_cost), 0).label("earned"),
        )
        .join(live, live.c.order_id == OrderDocument.order_id)
        .where(OrderDocument.notary_id.is_not(None))
        .group_by(OrderDocument.notary_id)
        .subquery("n_earned")
    )
    settled = (
        select(
            OrderDocument.notary_id.label("notary_id"),
            func.coalesce(func.sum(NotaryPaymentAllocation.amount_allocated), 0).label("paid"),
        )
        .join(OrderDocument, OrderDocument.id == NotaryPaymentAllocation.order_document_id)
        .where(OrderDocument.notary_id.is_not(None))
        .group_by(OrderDocument.notary_id)
        .subquery("n_settled")
    )
    earned_value = func.coalesce(earned.c.earned, 0)
    paid = func.coalesce(settled.c.paid, 0)
    return (
        select(
            Notary.id.label("notary_id"),
            Notary.name.label("name"),
            earned_value.label("earned"),
            paid.label("paid"),
            (earned_value - paid).label("outstanding"),
        )
        .outerjoin(earned, earned.c.notary_id == Notary.id)
        .outerjoin(settled, settled.c.notary_id == Notary.id)
        .subquery("notary_balances")
    )


def _per_order_owed() -> Any:
    """Per live order: client, date, billed, paid, owed."""
    live = _live_orders()
    docs = document_totals_subquery()
    paid = paid_subquery()
    billed = func.coalesce(docs.c.documents_total, 0) + Order.delivery_cost
    return (
        select(
            Order.id.label("order_id"),
            Order.client_id.label("client_id"),
            Order.order_date.label("order_date"),
            billed.label("billed"),
            func.coalesce(paid.c.paid, 0).label("paid"),
            (billed - func.coalesce(paid.c.paid, 0)).label("owed"),
        )
        .join(live, live.c.order_id == Order.id)
        .outerjoin(docs, docs.c.order_id == Order.id)
        .outerjoin(paid, paid.c.order_id == Order.id)
        .subquery("per_order")
    )


# ── Overview ────────────────────────────────────────────────────────────────


class PeriodCashFlow(BaseModel):
    start: date | None
    end: date | None
    payments_received: Decimal
    translator_payouts: Decimal
    #: Paid to notaries DIRECTLY. A fee the translator fronted is already
    #: inside their payout, and counting it again would double the outflow.
    notary_payouts: Decimal
    expenses: Decimal
    #: received minus (payouts + expenses). Negative is a loss-making period.
    net: Decimal


class FinanceOverview(BaseModel):
    """The cards across the top of /finances."""

    #: Owed to us by clients, across orders that are not fully paid.
    receivables: Decimal
    receivable_clients: int
    #: Owed by us to translators — the sum of each one's positive balance.
    payables_translators: Decimal
    #: Owed by us to notaries we can name.
    payables_notaries: Decimal
    #: Notary cost with no notary recorded — see the module docstring.
    unattributed_notary_fees: Decimal
    #: receivables minus (translator + notary payables).
    net_outstanding: Decimal
    period: PeriodCashFlow


async def _sum_in_period(
    db: AsyncSession,
    column: Any,
    date_column: Any,
    start: date | None,
    end: date | None,
    *conditions: Any,
) -> Decimal:
    stmt = select(func.coalesce(func.sum(column), 0)).where(*conditions)
    if start is not None:
        stmt = stmt.where(date_column >= start)
    if end is not None:
        stmt = stmt.where(date_column <= end)
    return _money(await db.scalar(stmt))


@router.get("/overview", response_model=FinanceOverview)
async def overview(
    db: Db,
    session: FinanceReader,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> FinanceOverview:
    # Default period is the current month — the bureau's month, not UTC's.
    if start is None and end is None:
        today = today_in(session.timezone)
        start, end = today.replace(day=1), today

    # Per order and not per client, because an overpaid order must not cancel
    # out an unpaid one — the client still owes the second.
    owed = _per_order_owed()
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

    translators = _translator_balances()
    payables_translators = _money(
        await db.scalar(
            select(func.coalesce(func.sum(translators.c.outstanding), 0)).where(
                translators.c.outstanding > 0
            )
        )
    )
    notaries = _notary_balances()
    payables_notaries = _money(
        await db.scalar(
            select(func.coalesce(func.sum(notaries.c.outstanding), 0)).where(
                notaries.c.outstanding > 0
            )
        )
    )

    live = _live_orders()
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
        db,
        NotaryPayment.amount,
        NotaryPayment.payment_date,
        start,
        end,
        NotaryPayment.paid_via_translator_payment_id.is_(None),
    )
    period_expenses = await _sum_in_period(db, Expense.amount, Expense.expense_date, start, end)

    receivables = _money(receivables_row[0])

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


class ClientAccount(BaseModel):
    """One client's whole position, for the client screen."""

    client_id: int
    #: Live orders (not cancelled or rejected), all of them.
    orders: int
    billed: Decimal
    paid: Decimal
    #: Sum of what each unpaid order still owes.
    outstanding: Decimal
    oldest_unpaid: date | None
    #: Received from this client and not allocated to any order yet.
    unallocated_credit: Decimal
    last_payment_date: date | None


class TranslatorBalance(BaseModel):
    translator_id: int
    translator_name: str
    earned: Decimal
    paid: Decimal
    #: Negative is credit: paid ahead of work, or for a job later cancelled.
    outstanding: Decimal


class NotaryBalance(BaseModel):
    notary_id: int
    notary_name: str
    earned: Decimal
    paid: Decimal
    outstanding: Decimal


@router.get("/receivables", response_model=list[ClientBalance])
async def receivables(
    db: Db,
    _: FinanceReader,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ClientBalance]:
    """Clients with an outstanding balance, largest first."""
    per_order = _per_order_owed()
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
            .order_by(func.sum(per_order.c.owed).desc(), per_order.c.client_id)
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


@router.get("/receivables/{client_id}", response_model=ClientAccount)
async def client_account(client_id: int, db: Db, _: FinanceReader) -> ClientAccount:
    """One client's balance, whether or not they owe anything.

    The list above only has clients who owe; the client screen needs the
    position of a client who is fully paid up too — "owes nothing" and
    "missing from the list" must not look the same.
    """
    if await db.get(Client, client_id) is None:
        raise NotFoundError("Client not found.")

    per_order = _per_order_owed()
    totals = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(per_order.c.billed), 0),
                func.coalesce(func.sum(per_order.c.paid), 0),
            ).where(per_order.c.client_id == client_id)
        )
    ).one()
    unpaid = (
        await db.execute(
            select(
                func.coalesce(func.sum(per_order.c.owed), 0), func.min(per_order.c.order_date)
            ).where(per_order.c.client_id == client_id, per_order.c.owed > 0)
        )
    ).one()
    received, last_payment = (
        await db.execute(
            select(
                func.coalesce(func.sum(ClientPayment.amount), 0),
                func.max(ClientPayment.payment_date),
            ).where(ClientPayment.client_id == client_id)
        )
    ).one()
    allocated = await db.scalar(
        select(func.coalesce(func.sum(ClientPaymentAllocation.amount_allocated), 0))
        .join(ClientPayment, ClientPayment.id == ClientPaymentAllocation.payment_id)
        .where(ClientPayment.client_id == client_id)
    )

    return ClientAccount(
        client_id=client_id,
        orders=int(totals[0] or 0),
        billed=_money(totals[1]),
        paid=_money(totals[2]),
        outstanding=_money(unpaid[0]),
        oldest_unpaid=unpaid[1],
        unallocated_credit=_money(received) - _money(allocated),
        last_payment_date=last_payment,
    )


@router.get("/payables", response_model=list[TranslatorBalance])
async def payables(
    db: Db,
    _: FinanceReader,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[TranslatorBalance]:
    """Translators owed money, largest first."""
    balances = _translator_balances()
    rows = (
        await db.execute(
            select(balances)
            .where(balances.c.outstanding > 0)
            .order_by(balances.c.outstanding.desc(), balances.c.translator_id)
            .limit(limit)
        )
    ).all()
    return [_translator_balance(row) for row in rows]


@router.get("/payables/{translator_id}", response_model=TranslatorBalance)
async def translator_balance(translator_id: int, db: Db, _: FinanceReader) -> TranslatorBalance:
    """One translator's balance — zero, credit or owed.

    The detail screen used to look the translator up in the list above, which
    only holds people who are owed; a fully-paid translator showed as having
    earned nothing and been paid nothing.
    """
    balances = _translator_balances()
    row = (
        await db.execute(select(balances).where(balances.c.translator_id == translator_id))
    ).first()
    if row is None:
        raise NotFoundError("Translator not found.")
    return _translator_balance(row)


def _translator_balance(row: Any) -> TranslatorBalance:
    return TranslatorBalance(
        translator_id=row.translator_id,
        translator_name=row.name,
        earned=_money(row.earned),
        paid=_money(row.paid),
        outstanding=_money(row.outstanding),
    )


@router.get("/notary-balances", response_model=list[NotaryBalance])
async def notary_balances(
    db: Db,
    _: FinanceReader,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NotaryBalance]:
    balances = _notary_balances()
    rows = (
        await db.execute(
            select(balances)
            .where(balances.c.outstanding > 0)
            .order_by(balances.c.outstanding.desc(), balances.c.notary_id)
            .limit(limit)
        )
    ).all()
    return [_notary_balance(row) for row in rows]


@router.get("/notary-balances/{notary_id}", response_model=NotaryBalance)
async def notary_balance(notary_id: int, db: Db, _: FinanceReader) -> NotaryBalance:
    balances = _notary_balances()
    row = (await db.execute(select(balances).where(balances.c.notary_id == notary_id))).first()
    if row is None:
        raise NotFoundError("Notary not found.")
    return _notary_balance(row)


def _notary_balance(row: Any) -> NotaryBalance:
    return NotaryBalance(
        notary_id=row.notary_id,
        notary_name=row.name,
        earned=_money(row.earned),
        paid=_money(row.paid),
        outstanding=_money(row.outstanding),
    )


# ── Expense ledger ──────────────────────────────────────────────────────────

#: Twin of `app.suliko.ge/src/features/finances/constants.ts`. Rent,
#: utilities and marketing are the PHP's categories the first cut dropped —
#: without them a bureau's biggest fixed costs had to be filed under "other".
EXPENSE_CATEGORIES = (
    "courier",
    "notary_office",
    "office",
    "rent",
    "utilities",
    "marketing",
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
    amount: PositiveMoney
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
    if search and search.strip():
        stmt = stmt.where(Expense.description.ilike(like_pattern(search), escape=LIKE_ESCAPE))
    return stmt


@router.get("/expenses", response_model=ExpensePage)
async def list_expenses(
    db: Db,
    _: FinanceReader,
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
    await db.flush()


# ── Idempotent inserts ──────────────────────────────────────────────────────


async def _insert_once(db: AsyncSession, row: Any, model: Any, key: str | None) -> Any | None:
    """Insert a ledger row, or return the one a retry already inserted.

    The lookup before the insert catches the ordinary retry. The savepoint and
    the unique index (`uq_*_idempotency`, revision 0008) catch the one the
    lookup cannot: two requests racing past each other. Returns the EXISTING
    row when the key was already used, None when `row` is new.
    """
    if key:
        existing = (
            (await db.execute(select(model).where(model.idempotency_key == key))).scalars().first()
        )
        if existing is not None:
            return existing
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        if not key:
            raise
        existing = (
            (await db.execute(select(model).where(model.idempotency_key == key))).scalars().first()
        )
        if existing is None:
            raise
        return existing
    return None


# ── Client payments ─────────────────────────────────────────────────────────


class Allocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int
    amount_allocated: PositiveMoney


class PaymentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: int
    amount: PositiveMoney
    payment_date: date
    method: PaymentMethod = PaymentMethod.BANK_TRANSFER
    notes: str | None = Field(default=None, max_length=255)
    #: How the payment is split across orders. One payment, N orders — this is
    #: how a client settling six invoices with one transfer is represented.
    allocations: list[Allocation] = Field(default_factory=list, max_length=200)
    #: Deduplicates a double-submitted form or a retried request. The form
    #: must create it ONCE per form, not per submit, or it deduplicates nothing.
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


async def _payments_out(db: AsyncSession, rows: list[ClientPayment]) -> list[PaymentOut]:
    """Several payments with their allocations and client names.

    Two queries for the whole page, not two per row.
    """
    if not rows:
        return []
    ids = [row.id for row in rows]
    allocations: dict[int, list[ClientPaymentAllocation]] = {}
    for allocation in (
        await db.execute(
            select(ClientPaymentAllocation)
            .where(ClientPaymentAllocation.payment_id.in_(ids))
            .order_by(ClientPaymentAllocation.id)
        )
    ).scalars():
        allocations.setdefault(allocation.payment_id, []).append(allocation)
    names = {
        int(client_id): name
        for client_id, name in (
            await db.execute(
                select(Client.id, Client.name).where(Client.id.in_({r.client_id for r in rows}))
            )
        ).all()
    }

    out: list[PaymentOut] = []
    for row in rows:
        mine = allocations.get(row.id, [])
        allocated = sum((a.amount_allocated for a in mine), ZERO)
        out.append(
            PaymentOut(
                id=row.id,
                client_id=row.client_id,
                client_name=names.get(row.client_id),
                amount=row.amount,
                payment_date=row.payment_date,
                method=row.method,
                notes=row.notes,
                recorded_by_user_id=row.recorded_by_user_id,
                allocations=[
                    AllocationOut(order_id=a.order_id, amount_allocated=a.amount_allocated)
                    for a in mine
                ],
                unallocated=row.amount - allocated,
            )
        )
    return out


async def _payment_out(db: AsyncSession, row: ClientPayment) -> PaymentOut:
    return (await _payments_out(db, [row]))[0]


@router.get("/payments", response_model=PaymentPage)
async def list_payments(
    db: Db,
    _: FinanceReader,
    client_id: Annotated[int | None, Query()] = None,
    order_id: Annotated[int | None, Query()] = None,
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
        if order_id is not None:
            # Payments with any part allocated to this order — the order
            # screen's Payments card.
            statement = statement.where(
                ClientPayment.id.in_(
                    select(ClientPaymentAllocation.payment_id).where(
                        ClientPaymentAllocation.order_id == order_id
                    )
                )
            )
        if start is not None:
            statement = statement.where(ClientPayment.payment_date >= start)
        if end is not None:
            statement = statement.where(ClientPayment.payment_date <= end)
        if method is not None:
            statement = statement.where(ClientPayment.method == method)
        if search and search.strip():
            pattern = like_pattern(search)
            statement = statement.where(
                or_(
                    ClientPayment.notes.ilike(pattern, escape=LIKE_ESCAPE),
                    ClientPayment.client_id.in_(
                        select(Client.id).where(Client.name.ilike(pattern, escape=LIKE_ESCAPE))
                    ),
                )
            )
        return statement

    counts = (await db.execute(filtered(count_stmt))).one()
    rows = list(
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
        items=await _payments_out(db, rows),
        meta=PageMeta(total=int(counts[0] or 0), limit=limit, offset=offset),
        total_amount=_money(counts[1]),
    )


async def _check_client_allocations(db: AsyncSession, payload: PaymentIn) -> None:
    """Each allocation: this client's order, still counting, and still owed.

    The last one is what stops 1,000 being allocated to an order that owes
    100 — the surplus would vanish into an order balance of -900 that every
    "who owes" query filters out as `owed > 0`, taking the money with it.
    """
    if not payload.allocations:
        return
    per_order = _per_order_owed()
    ids = [a.order_id for a in payload.allocations]
    live = {
        int(row.order_id): row
        for row in (await db.execute(select(per_order).where(per_order.c.order_id.in_(ids)))).all()
    }
    for allocation in payload.allocations:
        order = await db.get(Order, allocation.order_id)
        if order is None:
            raise NotFoundError(f"Order {allocation.order_id} not found.")
        # Allocating one client's payment to another client's order would
        # quietly move money between accounts.
        if order.client_id != payload.client_id:
            raise ValidationError(f"Order {allocation.order_id} belongs to a different client.")
        row = live.get(allocation.order_id)
        if row is None:
            raise ValidationError(
                f"Order {allocation.order_id} is cancelled or rejected. "
                "Record the money without allocating it, or reopen the order."
            )
        owed = _money(row.owed)
        if allocation.amount_allocated > owed:
            raise ValidationError(
                f"Order {allocation.order_id} has only {owed} outstanding; "
                f"{allocation.amount_allocated} cannot be allocated to it. "
                "Leave the rest unallocated."
            )


@router.post("/payments", response_model=PaymentOut, status_code=http_status.HTTP_201_CREATED)
async def record_payment(payload: PaymentIn, db: Db, session: PaymentRecorder) -> PaymentOut:
    if await db.get(Client, payload.client_id) is None:
        raise NotFoundError("Client not found.")

    payment = ClientPayment(
        client_id=payload.client_id,
        amount=payload.amount,
        payment_date=payload.payment_date,
        method=payload.method,
        notes=payload.notes,
        recorded_by_user_id=session.user_id,
        idempotency_key=payload.idempotency_key,
    )
    # Before validating allocations: a retry of a payment that already went
    # through must answer with it, not with "that order no longer owes".
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

    await _check_client_allocations(db, payload)

    existing = await _insert_once(db, payment, ClientPayment, payload.idempotency_key)
    if existing is not None:
        return await _payment_out(db, existing)

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
    # Only to people who may see finances: an amount is exactly what the
    # Finances screen withholds from everyone else.
    await notify_permitted(
        db,
        permission=Permission.FINANCE_READ,
        plan=session.plan,
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
async def delete_payment(payment_id: int, db: Db, session: MoneyUndoer) -> None:
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
    await db.flush()


# ── Supplier payouts ────────────────────────────────────────────────────────


class PayoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: PositiveMoney
    payment_date: date
    method: PaymentMethod = PaymentMethod.BANK_TRANSFER
    notes: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=64)


class TranslatorPayoutIn(PayoutIn):
    translator_id: int
    #: Which orders this payout settles. Same one-payment-N-allocations shape
    #: as client payments. Optional: an unallocated payout still reduces what
    #: the translator is owed (as the PHP counts it) — allocating only says
    #: which jobs it was for.
    allocations: list[Allocation] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _allocations_fit(self) -> TranslatorPayoutIn:
        allocated = sum((a.amount_allocated for a in self.allocations), ZERO)
        if allocated > self.amount:
            raise ValueError(f"Allocations total {allocated} but the payout is only {self.amount}.")
        if len({a.order_id for a in self.allocations}) != len(self.allocations):
            raise ValueError("The same order appears twice in the allocations.")
        return self


class PayoutOut(BaseModel):
    id: int
    amount: Decimal
    payment_date: date
    method: PaymentMethod
    notes: str | None
    unallocated: Decimal


class TranslatorPayoutOut(PayoutOut):
    translator_id: int
    translator_name: str | None
    allocations: list[AllocationOut]
    #: Of this payout, how much reimbursed notary fees the translator fronted.
    notary_reimbursed: Decimal


class NotaryPayoutAllocationOut(BaseModel):
    order_document_id: int
    order_id: int | None
    notary_id: int | None
    notary_name: str | None
    amount_allocated: Decimal


class NotaryPayoutOut(PayoutOut):
    paid_via_translator_payment_id: int | None
    allocations: list[NotaryPayoutAllocationOut]


async def _check_translator_allocations(db: AsyncSession, payload: TranslatorPayoutIn) -> None:
    """Each allocation: an order this translator worked on, not overpaid.

    Allocating a payout to a job someone else translated would pay one
    translator for another's work and leave both balances wrong.
    """
    for allocation in payload.allocations:
        if await db.get(Order, allocation.order_id) is None:
            raise NotFoundError(f"Order {allocation.order_id} not found.")
        their_cost = await db.scalar(
            select(func.coalesce(func.sum(OrderDocument.translator_cost), 0)).where(
                OrderDocument.order_id == allocation.order_id,
                OrderDocument.translator_id == payload.translator_id,
            )
        )
        if not their_cost:
            raise ValidationError(
                f"Order {allocation.order_id} has no documents by this translator."
            )
        already = await db.scalar(
            select(func.coalesce(func.sum(TranslatorPaymentAllocation.amount_allocated), 0))
            .join(
                TranslatorPayment,
                TranslatorPayment.id == TranslatorPaymentAllocation.payment_id,
            )
            .where(
                TranslatorPaymentAllocation.order_id == allocation.order_id,
                TranslatorPayment.translator_id == payload.translator_id,
            )
        )
        remaining = _money(their_cost) - _money(already)
        if allocation.amount_allocated > remaining:
            raise ValidationError(
                f"Order {allocation.order_id}: this translator is owed {remaining} for it; "
                f"{allocation.amount_allocated} cannot be allocated. Leave the rest unallocated."
            )


async def _translator_payouts_out(
    db: AsyncSession, rows: list[TranslatorPayment]
) -> list[TranslatorPayoutOut]:
    if not rows:
        return []
    ids = [row.id for row in rows]
    allocations: dict[int, list[TranslatorPaymentAllocation]] = {}
    for allocation in (
        await db.execute(
            select(TranslatorPaymentAllocation)
            .where(TranslatorPaymentAllocation.payment_id.in_(ids))
            .order_by(TranslatorPaymentAllocation.id)
        )
    ).scalars():
        allocations.setdefault(allocation.payment_id, []).append(allocation)
    reimbursed = {
        int(via): _money(total)
        for via, total in (
            await db.execute(
                select(NotaryPayment.paid_via_translator_payment_id, func.sum(NotaryPayment.amount))
                .where(NotaryPayment.paid_via_translator_payment_id.in_(ids))
                .group_by(NotaryPayment.paid_via_translator_payment_id)
            )
        ).all()
    }
    names = {
        int(tid): name
        for tid, name in (
            await db.execute(
                select(Translator.id, Translator.name).where(
                    Translator.id.in_({r.translator_id for r in rows})
                )
            )
        ).all()
    }
    return [
        TranslatorPayoutOut(
            id=row.id,
            translator_id=row.translator_id,
            translator_name=names.get(row.translator_id),
            amount=row.amount,
            payment_date=row.payment_date,
            method=row.method,
            notes=row.notes,
            allocations=[
                AllocationOut(order_id=a.order_id, amount_allocated=a.amount_allocated)
                for a in allocations.get(row.id, [])
            ],
            unallocated=row.amount
            - sum((a.amount_allocated for a in allocations.get(row.id, [])), ZERO),
            notary_reimbursed=reimbursed.get(row.id, ZERO),
        )
        for row in rows
    ]


@router.get("/translator-payouts", response_model=list[TranslatorPayoutOut])
async def list_translator_payouts(
    db: Db,
    _: FinanceReader,
    translator_id: Annotated[int | None, Query()] = None,
    order_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TranslatorPayoutOut]:
    stmt = select(TranslatorPayment)
    if translator_id is not None:
        stmt = stmt.where(TranslatorPayment.translator_id == translator_id)
    if order_id is not None:
        stmt = stmt.where(
            TranslatorPayment.id.in_(
                select(TranslatorPaymentAllocation.payment_id).where(
                    TranslatorPaymentAllocation.order_id == order_id
                )
            )
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(TranslatorPayment.payment_date.desc(), TranslatorPayment.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return await _translator_payouts_out(db, rows)


@router.post(
    "/translator-payouts", response_model=PayoutOut, status_code=http_status.HTTP_201_CREATED
)
async def pay_translator(
    payload: TranslatorPayoutIn, db: Db, session: PaymentRecorder
) -> PayoutOut:
    if await db.get(Translator, payload.translator_id) is None:
        raise NotFoundError("Translator not found.")

    payout = TranslatorPayment(
        translator_id=payload.translator_id,
        amount=payload.amount,
        payment_date=payload.payment_date,
        method=payload.method,
        notes=payload.notes,
        recorded_by_user_id=session.user_id,
        idempotency_key=payload.idempotency_key,
    )
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
            return (await _translator_payouts_out(db, [existing]))[0]

    await _check_translator_allocations(db, payload)

    existing = await _insert_once(db, payout, TranslatorPayment, payload.idempotency_key)
    if existing is not None:
        return (await _translator_payouts_out(db, [existing]))[0]

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

    return (await _translator_payouts_out(db, [payout]))[0]


@router.delete(
    "/translator-payouts/{payout_id}", status_code=http_status.HTTP_204_NO_CONTENT
)
async def delete_translator_payout(payout_id: int, db: Db, session: MoneyUndoer) -> None:
    row = await db.get(TranslatorPayment, payout_id)
    if row is None:
        raise NotFoundError("Payout not found.")

    via = await db.scalar(
        select(func.count())
        .select_from(NotaryPayment)
        .where(NotaryPayment.paid_via_translator_payment_id == payout_id)
    )
    if via:
        # The foreign key would quietly unlink them (SET NULL), turning a fee
        # the translator fronted into one the office paid the notary itself.
        raise ConflictError(
            "A notary fee was reimbursed through this payout. Remove that notary payment first."
        )

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="translator_payout.deleted",
        entity_type="translator_payment",
        entity_id=row.id,
        before={
            "translator_id": row.translator_id,
            "amount": str(row.amount),
            "payment_date": row.payment_date.isoformat(),
        },
    )
    await db.delete(row)
    await db.flush()


class NotaryPayoutIn(PayoutIn):
    #: Which documents this settles. Notary cost lives on the document, not
    #: the order, so allocations key on documents. Do not "simplify" this.
    document_ids: list[int] = Field(default_factory=list, max_length=200)
    amounts: list[PositiveMoney] = Field(default_factory=list, max_length=200)
    #: Set when the translator fronted the cash and is reimbursed in the same
    #: transfer as their translation fee.
    paid_via_translator_payment_id: int | None = None

    @model_validator(mode="after")
    def _parallel_lists(self) -> NotaryPayoutIn:
        if len(self.document_ids) != len(self.amounts):
            raise ValueError("document_ids and amounts must be the same length.")
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("The same document appears twice in the allocations.")
        allocated = sum(self.amounts, ZERO)
        if allocated > self.amount:
            raise ValueError(f"Allocations total {allocated} but the payout is only {self.amount}.")
        return self


async def _check_notary_allocations(db: AsyncSession, payload: NotaryPayoutIn) -> None:
    for document_id, amount in zip(payload.document_ids, payload.amounts, strict=True):
        document = await db.get(OrderDocument, document_id)
        if document is None:
            raise NotFoundError(f"Document {document_id} not found.")
        if not document.is_notarized:
            raise ValidationError(f"Document {document_id} is not being notarised.")
        already = await db.scalar(
            select(func.coalesce(func.sum(NotaryPaymentAllocation.amount_allocated), 0)).where(
                NotaryPaymentAllocation.order_document_id == document_id
            )
        )
        remaining = _money(document.notary_cost) - _money(already)
        if amount > remaining:
            raise ValidationError(
                f"Document {document_id} has {remaining} of notary fee outstanding; "
                f"{amount} cannot be allocated to it."
            )


async def _notary_payouts_out(
    db: AsyncSession, rows: list[NotaryPayment]
) -> list[NotaryPayoutOut]:
    if not rows:
        return []
    ids = [row.id for row in rows]
    allocations: dict[int, list[NotaryPayoutAllocationOut]] = {}
    for allocation, order_id, notary_id, notary_name in (
        await db.execute(
            select(
                NotaryPaymentAllocation,
                OrderDocument.order_id,
                OrderDocument.notary_id,
                Notary.name,
            )
            .join(OrderDocument, OrderDocument.id == NotaryPaymentAllocation.order_document_id)
            .outerjoin(Notary, Notary.id == OrderDocument.notary_id)
            .where(NotaryPaymentAllocation.payment_id.in_(ids))
            .order_by(NotaryPaymentAllocation.id)
        )
    ).all():
        allocations.setdefault(allocation.payment_id, []).append(
            NotaryPayoutAllocationOut(
                order_document_id=allocation.order_document_id,
                order_id=order_id,
                notary_id=notary_id,
                notary_name=notary_name,
                amount_allocated=allocation.amount_allocated,
            )
        )
    return [
        NotaryPayoutOut(
            id=row.id,
            amount=row.amount,
            payment_date=row.payment_date,
            method=row.method,
            notes=row.notes,
            paid_via_translator_payment_id=row.paid_via_translator_payment_id,
            allocations=allocations.get(row.id, []),
            unallocated=row.amount
            - sum((a.amount_allocated for a in allocations.get(row.id, [])), ZERO),
        )
        for row in rows
    ]


@router.get("/notary-payouts", response_model=list[NotaryPayoutOut])
async def list_notary_payouts(
    db: Db,
    _: FinanceReader,
    order_id: Annotated[int | None, Query()] = None,
    notary_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[NotaryPayoutOut]:
    stmt = select(NotaryPayment)
    if order_id is not None or notary_id is not None:
        allocated_to = select(NotaryPaymentAllocation.payment_id).join(
            OrderDocument, OrderDocument.id == NotaryPaymentAllocation.order_document_id
        )
        if order_id is not None:
            allocated_to = allocated_to.where(OrderDocument.order_id == order_id)
        if notary_id is not None:
            allocated_to = allocated_to.where(OrderDocument.notary_id == notary_id)
        stmt = stmt.where(NotaryPayment.id.in_(allocated_to))
    rows = list(
        (
            await db.execute(
                stmt.order_by(NotaryPayment.payment_date.desc(), NotaryPayment.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return await _notary_payouts_out(db, rows)


@router.post("/notary-payouts", response_model=PayoutOut, status_code=http_status.HTTP_201_CREATED)
async def pay_notary(payload: NotaryPayoutIn, db: Db, session: PaymentRecorder) -> PayoutOut:
    if payload.idempotency_key:
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
        # The one ledger whose key was stored and never looked up: a
        # double-submitted notary payout was recorded twice.
        if existing is not None:
            return (await _notary_payouts_out(db, [existing]))[0]

    if payload.paid_via_translator_payment_id is not None:
        via = await db.get(TranslatorPayment, payload.paid_via_translator_payment_id)
        if via is None:
            raise NotFoundError("The translator payment it was paid through was not found.")

    await _check_notary_allocations(db, payload)

    payout = NotaryPayment(
        amount=payload.amount,
        payment_date=payload.payment_date,
        method=payload.method,
        notes=payload.notes,
        paid_via_translator_payment_id=payload.paid_via_translator_payment_id,
        recorded_by_user_id=session.user_id,
        idempotency_key=payload.idempotency_key,
    )
    existing = await _insert_once(db, payout, NotaryPayment, payload.idempotency_key)
    if existing is not None:
        return (await _notary_payouts_out(db, [existing]))[0]

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

    return (await _notary_payouts_out(db, [payout]))[0]


@router.delete("/notary-payouts/{payout_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_notary_payout(payout_id: int, db: Db, session: MoneyUndoer) -> None:
    row = await db.get(NotaryPayment, payout_id)
    if row is None:
        raise NotFoundError("Payout not found.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="notary_payout.deleted",
        entity_type="notary_payment",
        entity_id=row.id,
        before={
            "amount": str(row.amount),
            "payment_date": row.payment_date.isoformat(),
            "paid_via_translator_payment_id": row.paid_via_translator_payment_id,
        },
    )
    # Allocations cascade.
    await db.delete(row)
    await db.flush()
