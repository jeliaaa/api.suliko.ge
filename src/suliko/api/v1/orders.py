"""Orders — the core resource.

The PHP calls these "translations"; they are renamed here because an order is
the commercial object and a translation is one of the things done to it.

Creating an order prices every document through `suliko.domain.pricing` and
stores the result. Costs are STORED, not recomputed on read: a rate change must
never retroactively alter what an existing order charged, and staff routinely
hand-adjust `price`, `translator_cost` and `notary_cost` — on create through
the `*_override` fields, afterwards through `PATCH .../documents/{id}`.

Status is append-only. There is no `status` column — the current status is the
latest row in `order_status_events`. A denormalised column would be a second
source of truth that eventually disagrees with the history.

## Who sees what it cost

Price and what the client paid are operational — every role that reads
orders needs them. What the job COST (translator and notary fees, order
expenses) and therefore the profit is not: staff get the order without it
unless they hold `reports.profit`. Stripped here, on the server, because the
frontend hiding a column is presentation, not access control.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from suliko.api.deps import Db, require
from suliko.api.v1._shared import (
    LIKE_ESCAPE,
    Money,
    PageMeta,
    digits_of,
    like_pattern,
    phone_digits,
)
from suliko.core.errors import ConflictError, NotFoundError, ValidationError
from suliko.domain.clock import add_days, today_in
from suliko.domain.notifications import notify, notify_everyone
from suliko.domain.orders import base_order_query
from suliko.domain.pricing import PriceBreakdown
from suliko.domain.pricing_context import PricingContext, load_pricing_context
from suliko.domain.statuses import (
    CLOSED_STATUSES,
    EXCLUDED_FROM_AGGREGATES,
    INITIAL_STATUS,
    get_label,
    is_known,
    sql_values,
)
from suliko.models.collaboration import NotificationKind
from suliko.models.directory import Client, ClientType, Notary, Translator
from suliko.models.finance import (
    ClientPaymentAllocation,
    Expense,
    NotaryPaymentAllocation,
    TranslatorPaymentAllocation,
)
from suliko.models.order import (
    CopyType,
    HandoverMethod,
    Order,
    OrderDocument,
    OrderStatusEvent,
    Urgency,
)
from suliko.models.user import User
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

router = APIRouter(prefix="/orders", tags=["orders"])

MAX_DOCUMENTS = 50
NOTES_MAX = 5000

OrdersReader = Annotated[AuthenticatedSession, Depends(require(Permission.ORDERS_READ))]
OrdersWriter = Annotated[AuthenticatedSession, Depends(require(Permission.ORDERS_WRITE))]


# ── Schemas ─────────────────────────────────────────────────────────────────


class OrderDocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type_id: int
    source_language: str = Field(min_length=2, max_length=5)
    target_language: str = Field(min_length=2, max_length=5)
    page_count: int = Field(ge=1, le=10_000)
    copy_type: CopyType = CopyType.ORIGINAL
    #: Overrides what `copy_type` implies. Staff notarise a translation of an
    #: original, or waive notarisation on a notary copy type. None = derive.
    is_notarized: bool | None = None
    translator_id: int | None = None
    notary_id: int | None = None

    #: Override the computed figures. Staff negotiate, translators have their
    #: own rates and notary offices round; the calculated figure is a starting
    #: point rather than a rule.
    price_override: Money | None = None
    translator_cost_override: Money | None = None
    notary_cost_override: Money | None = None


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: int
    documents: list[OrderDocumentIn] = Field(min_length=1, max_length=MAX_DOCUMENTS)
    order_date: date | None = None
    #: Omitted -> order date plus the tenant's days for this urgency.
    due_date: date | None = None
    contact_info: str | None = Field(default=None, max_length=255)
    urgency: Urgency = Urgency.STANDARD
    handover_method: HandoverMethod = HandoverMethod.SCAN
    delivery_address: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=NOTES_MAX)


class OrderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_date: date | None = None
    due_date: date | None = None
    contact_info: str | None = Field(default=None, max_length=255)
    urgency: Urgency | None = None
    handover_method: HandoverMethod | None = None
    delivery_address: str | None = Field(default=None, max_length=500)
    #: Omitted with a handover change -> the tenant's fee or zero.
    delivery_cost: Money | None = None
    notes: str | None = Field(default=None, max_length=NOTES_MAX)


class OrderDocumentUpdate(BaseModel):
    """Change one document after the order was created.

    Only the fields present are applied. ``{"translator_id": null}`` unassigns;
    ``{}`` changes nothing.

    Changing what is being translated — type, languages, pages, copy type,
    notarisation — re-prices the document, EXCEPT for any of price,
    translator_cost and notary_cost sent in the same request, which win. Send
    only what changed: an unchanged price echoed back reads as "keep this".
    """

    model_config = ConfigDict(extra="forbid")

    document_type_id: int | None = None
    source_language: str | None = Field(default=None, min_length=2, max_length=5)
    target_language: str | None = Field(default=None, min_length=2, max_length=5)
    page_count: int | None = Field(default=None, ge=1, le=10_000)
    copy_type: CopyType | None = None
    is_notarized: bool | None = None
    translator_id: int | None = None
    notary_id: int | None = None
    price: Money | None = None
    translator_cost: Money | None = None
    notary_cost: Money | None = None


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=60)
    note: str | None = Field(default=None, max_length=500)


class OrderDocumentOut(BaseModel):
    id: int
    document_type_id: int
    document_type_name: str | None
    document_type_name_ka: str | None
    source_language: str
    target_language: str
    page_count: int
    copy_type: CopyType
    is_notarized: bool
    price: Decimal
    #: Null without `reports.profit` — see the module docstring.
    translator_cost: Decimal | None
    notary_cost: Decimal | None
    translator_id: int | None
    translator_name: str | None
    notary_id: int | None
    notary_name: str | None


class OrderSummary(BaseModel):
    id: int
    order_date: date
    due_date: date | None
    client_id: int
    client_name: str
    client_type: ClientType
    status: str | None
    status_label: str
    urgency: Urgency
    document_count: int
    page_count: int
    #: Documents plus delivery — what the client owes.
    total: Decimal
    paid: Decimal
    #: After translator, notary and order expenses. Delivery is passed
    #: through to the courier and is NOT profit (decided 2026-09-24, as the
    #: PHP). Null without `reports.profit`.
    profit: Decimal | None
    #: unpaid | partial | paid — drives the tri-state icon next to Price.
    paid_state: Literal["unpaid", "partial", "paid"]
    #: Past due and still open. A completed order is never overdue.
    is_overdue: bool
    #: Filled on the list only: who is translating it and which language
    #: pairs, as the PHP list showed them — "ka→en". One batched query per
    #: page, not one per row.
    translator_names: list[str] = Field(default_factory=list)
    language_pairs: list[str] = Field(default_factory=list)


class StatusEventOut(BaseModel):
    status: str
    status_label: str
    changed_at: datetime
    changed_by_user_id: int | None
    changed_by_name: str | None
    note: str | None


class OrderDetail(OrderSummary):
    contact_info: str | None
    handover_method: HandoverMethod
    delivery_address: str | None
    delivery_cost: Decimal
    source: str | None
    notes: str | None
    documents_total: Decimal
    translator_total: Decimal | None
    notary_total: Decimal | None
    expenses_total: Decimal | None
    documents: list[OrderDocumentOut]
    status_history: list[StatusEventOut]


class OrderPage(BaseModel):
    items: list[OrderSummary]
    meta: PageMeta


# ── Helpers ─────────────────────────────────────────────────────────────────


def shows_costs(session: AuthenticatedSession) -> bool:
    return session.has(Permission.REPORTS_PROFIT)


def _paid_state(total: Decimal, paid: Decimal) -> Literal["unpaid", "partial", "paid"]:
    """Port of the PHP's `paid_status_icon_html`.

    Compared as Decimal, so a fully-paid order is never reported as partial
    because of float dust. Nothing owed is "paid", not "unpaid" — a free job
    is not a debt.
    """
    if total <= 0 or paid >= total:
        return "paid"
    if paid <= 0:
        return "unpaid"
    return "partial"


def _summary_from_row(row: Row[Any], *, today: date, show_costs: bool) -> OrderSummary:
    """Build a summary from one `base_order_query` row.

    The row is a plain tuple in the column order `base_order_query` selects;
    keeping that shape in one place means callers never index into it.
    """
    order: Order = row[0]
    status_value: str | None = row[1]

    documents_total = Decimal(row[2] or 0)
    gross_profit = Decimal(row[5] or 0)
    document_count = int(row[6] or 0)
    page_count = int(row[7] or 0)
    paid = Decimal(row[8] or 0)
    expenses = Decimal(row[9] or 0)

    total = documents_total + order.delivery_cost
    closed = (status_value or "").strip().lower() in CLOSED_STATUSES

    return OrderSummary(
        id=order.id,
        order_date=order.order_date,
        due_date=order.due_date,
        client_id=order.client_id,
        client_name=order.client.name if order.client else "",
        client_type=order.client.client_type if order.client else ClientType.B2C,
        status=status_value,
        status_label=get_label(status_value),
        urgency=order.urgency,
        document_count=document_count,
        page_count=page_count,
        total=total,
        paid=paid,
        profit=(gross_profit - expenses) if show_costs else None,
        paid_state=_paid_state(total, paid),
        is_overdue=bool(order.due_date and order.due_date < today and not closed),
    )


async def _check_assignees(
    db: AsyncSession,
    session: AuthenticatedSession,
    *,
    translator_id: int | None,
    notary_id: int | None,
) -> Translator | None:
    """Refuse a translator or notary this bureau does not have.

    `db.get` runs under the tenant filter, so another bureau's id comes back
    None exactly as a made-up one does — the caller cannot tell them apart,
    and nothing from another tenant is ever stored on this one's order.
    """
    translator: Translator | None = None
    if translator_id is not None:
        if not session.has(Permission.TRANSLATORS_READ):
            # A freelancer's plan has no translators: they ARE the translator.
            raise ValidationError("Your plan does not include assigning translators.")
        translator = await db.get(Translator, translator_id)
        if translator is None:
            raise ValidationError("That translator does not exist.")
    if notary_id is not None and await db.get(Notary, notary_id) is None:
        raise ValidationError("That notary does not exist.")
    return translator


def _translator_default_cost(translator: Translator | None, page_count: int) -> Decimal | None:
    """The translator's own per-page rate times pages, when they have one.

    How the PHP office screen priced a translator — their rate, not a share
    of the client price. Only a default: an explicit cost always wins.
    """
    if translator is None or translator.default_rate is None:
        return None
    return (Decimal(str(translator.default_rate)) * page_count).quantize(Decimal("0.01"))


def _require_rate(ctx: PricingContext, source: str, target: str, *, has_price: bool) -> None:
    """No silent fallback rate on a stored order.

    The engine falls back to 15 GEL/page for an unpriced pair, which is fine
    for a quote that says so on screen and wrong for an order the client is
    billed from. Either the pair has a rate or the price was typed in.
    """
    if not has_price and ctx.rate(source, target) is None:
        raise ValidationError(
            f"No price is set for {source.lower()} → {target.lower()}. "
            "Add it in Settings → Pricing, or enter the price for this document by hand."
        )


async def _changed_by_names(db: AsyncSession, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    rows = await db.execute(select(User.id, User.full_name).where(User.id.in_(user_ids)))
    return {int(user_id): name for user_id, name in rows.all()}


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=OrderPage)
def list_extras(
    rows: list[tuple[int, str, str, str | None]],
) -> dict[int, tuple[list[str], list[str]]]:
    """Group `(order_id, source, target, translator name)` rows per order.

    First-seen order is kept and duplicates dropped, so an order of five
    ka→en documents by one translator reads "ka→en" and one name.
    """
    extras: dict[int, tuple[list[str], list[str]]] = {}
    for order_id, source, target, translator in rows:
        names, pairs = extras.setdefault(order_id, ([], []))
        pair = f"{source}→{target}"
        if pair not in pairs:
            pairs.append(pair)
        if translator and translator not in names:
            names.append(translator)
    return extras


async def _load_list_extras(
    db: AsyncSession, order_ids: list[int]
) -> dict[int, tuple[list[str], list[str]]]:
    if not order_ids:
        return {}
    result = await db.execute(
        select(
            OrderDocument.order_id,
            OrderDocument.source_language,
            OrderDocument.target_language,
            Translator.name,
        )
        .outerjoin(Translator, Translator.id == OrderDocument.translator_id)
        .where(OrderDocument.order_id.in_(order_ids))
        .order_by(OrderDocument.order_id, OrderDocument.id)
    )
    return list_extras([(r[0], r[1], r[2], r[3]) for r in result.all()])


async def list_orders(
    db: Db,
    session: OrdersReader,
    search: Annotated[str | None, Query(max_length=255)] = None,
    client_type: ClientType | None = None,
    order_status: Annotated[str | None, Query(alias="status", max_length=60)] = None,
    language: Annotated[str | None, Query(max_length=5)] = None,
    client_id: int | None = None,
    translator_id: int | None = None,
    document_type_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    overdue: bool = False,
    due_today: bool = False,
    unpaid: bool = False,
    mine: bool = False,
    sort: Literal["date", "-date", "id", "-id", "due", "-due"] = "-id",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderPage:
    stmt, status_sq, docs_sq, paid_sq, _expenses = base_order_query()
    today = today_in(session.timezone)

    # The client is joined rather than lazy-loaded: the list shows a client
    # name on every row, and lazy loading would be one query per row.
    from sqlalchemy.orm import joinedload

    stmt = stmt.join(Client, Client.id == Order.client_id).options(joinedload(Order.client))

    def has_document(*conditions: ColumnElement[bool]) -> ColumnElement[bool]:
        return (
            select(OrderDocument.id)
            .where(OrderDocument.order_id == Order.id, *conditions)
            .exists()
        )

    if search and search.strip():
        pattern = like_pattern(search)
        conditions: list[ColumnElement[bool]] = [
            Client.name.ilike(pattern, escape=LIKE_ESCAPE),
            Client.email.ilike(pattern, escape=LIKE_ESCAPE),
            Client.personal_number.ilike(pattern, escape=LIKE_ESCAPE),
            Order.contact_info.ilike(pattern, escape=LIKE_ESCAPE),
            # Who is translating it — the question the office phone usually asks.
            has_document(
                OrderDocument.translator_id.in_(
                    select(Translator.id).where(Translator.name.ilike(pattern, escape=LIKE_ESCAPE))
                )
            ),
        ]
        digits = digits_of(search)
        # Phones are stored as typed; compare digits to digits.
        if len(digits) >= 4:
            conditions.append(phone_digits(Client.phone).like(f"%{digits}%"))
        # A bare number is almost always an order id, so match it as one too.
        if search.strip().isdigit():
            conditions.append(Order.id == int(search.strip()))
        stmt = stmt.where(or_(*conditions))

    if client_type is not None:
        stmt = stmt.where(Client.client_type == client_type)
    if client_id is not None:
        stmt = stmt.where(Order.client_id == client_id)
    if translator_id is not None:
        stmt = stmt.where(has_document(OrderDocument.translator_id == translator_id))
    if document_type_id is not None:
        stmt = stmt.where(has_document(OrderDocument.document_type_id == document_type_id))
    if order_status:
        stmt = stmt.where(status_sq.c.status == order_status.strip().lower())
    if language:
        # Any document in the order using this language, either direction.
        code = language.lower()
        stmt = stmt.where(
            has_document(
                or_(OrderDocument.source_language == code, OrderDocument.target_language == code)
            )
        )
    if date_from:
        stmt = stmt.where(Order.order_date >= date_from)
    if date_to:
        stmt = stmt.where(Order.order_date <= date_to)

    current_status = func.coalesce(status_sq.c.status, INITIAL_STATUS)
    still_open = current_status.notin_(sql_values(CLOSED_STATUSES))
    if overdue:
        stmt = stmt.where(Order.due_date < today, still_open)
    if due_today:
        stmt = stmt.where(Order.due_date == today, still_open)
    if unpaid:
        owed = func.coalesce(docs_sq.c.documents_total, 0) + Order.delivery_cost
        stmt = stmt.where(
            func.coalesce(paid_sq.c.paid, 0) < owed,
            current_status.notin_(sql_values(EXCLUDED_FROM_AGGREGATES)),
        )
    if mine:
        stmt = stmt.where(Order.created_by_user_id == session.user_id)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    column = {
        "date": Order.order_date,
        "due": Order.due_date,
        "id": Order.id,
    }[sort.lstrip("-")]
    descending = sort.startswith("-")
    primary = column.desc() if descending else column.asc()
    if sort.lstrip("-") == "due":
        # No due date sorts last either way — "latest due first" should not
        # open on a page of undated orders.
        primary = primary.nulls_last()
    # The id tie-break is what makes offset paging stable. Twenty orders on
    # one date otherwise come back in whatever order the planner likes, and
    # the same row can appear on page 1 and page 2 while another never does.
    stmt = stmt.order_by(primary, Order.id.desc() if descending else Order.id.asc())

    rows = (await db.execute(stmt.limit(limit).offset(offset))).unique().all()
    extras = await _load_list_extras(db, [r[0].id for r in rows])

    items = []
    for r in rows:
        summary = _summary_from_row(r, today=today, show_costs=shows_costs(session))
        names, pairs = extras.get(summary.id, ([], []))
        summary.translator_names = names
        summary.language_pairs = pairs
        items.append(summary)

    return OrderPage(items=items, meta=PageMeta(total=total, limit=limit, offset=offset))


async def _load_detail(
    order_id: int, db: AsyncSession, session: AuthenticatedSession
) -> OrderDetail:
    """Load one order with everything the detail screen shows.

    Separate from the route handler so create, update and status-change can
    return the fresh state without calling a handler as a function and passing
    None where a dependency belongs.
    """
    from sqlalchemy.orm import joinedload

    stmt, _status, _docs, _paid, _expenses = base_order_query()
    stmt = stmt.options(joinedload(Order.client)).where(Order.id == order_id)

    row = (await db.execute(stmt)).unique().first()
    if row is None:
        raise NotFoundError("Order not found.")

    show_costs = shows_costs(session)
    summary = _summary_from_row(row, today=today_in(session.timezone), show_costs=show_costs)
    order = row[0]

    documents = (
        (
            await db.execute(
                select(OrderDocument)
                .where(OrderDocument.order_id == order_id)
                .options(
                    joinedload(OrderDocument.document_type),
                    joinedload(OrderDocument.translator),
                    joinedload(OrderDocument.notary),
                )
                .order_by(OrderDocument.id)
            )
        )
        .unique()
        .scalars()
        .all()
    )

    history = (
        (
            await db.execute(
                select(OrderStatusEvent)
                .where(OrderStatusEvent.order_id == order_id)
                .order_by(OrderStatusEvent.changed_at.desc(), OrderStatusEvent.id.desc())
            )
        )
        .scalars()
        .all()
    )
    names = await _changed_by_names(
        db, {e.changed_by_user_id for e in history if e.changed_by_user_id is not None}
    )

    def cost(value: Decimal) -> Decimal | None:
        return value if show_costs else None

    return OrderDetail(
        **summary.model_dump(),
        contact_info=order.contact_info,
        handover_method=order.handover_method,
        delivery_address=order.delivery_address,
        delivery_cost=order.delivery_cost,
        source=order.source,
        notes=order.notes,
        documents_total=Decimal(row[2] or 0),
        translator_total=cost(Decimal(row[3] or 0)),
        notary_total=cost(Decimal(row[4] or 0)),
        expenses_total=cost(Decimal(row[9] or 0)),
        documents=[
            OrderDocumentOut(
                id=d.id,
                document_type_id=d.document_type_id,
                document_type_name=d.document_type.name_en if d.document_type else None,
                document_type_name_ka=d.document_type.name_ka if d.document_type else None,
                source_language=d.source_language,
                target_language=d.target_language,
                page_count=d.page_count,
                copy_type=d.copy_type,
                is_notarized=d.is_notarized,
                price=d.price,
                translator_cost=cost(d.translator_cost),
                notary_cost=cost(d.notary_cost),
                translator_id=d.translator_id,
                translator_name=d.translator.name if d.translator else None,
                notary_id=d.notary_id,
                notary_name=d.notary.name if d.notary else None,
            )
            for d in documents
        ],
        status_history=[
            StatusEventOut(
                status=e.status,
                status_label=get_label(e.status),
                changed_at=e.changed_at,
                changed_by_user_id=e.changed_by_user_id,
                changed_by_name=names.get(e.changed_by_user_id)
                if e.changed_by_user_id is not None
                else None,
                note=e.note,
            )
            for e in history
        ],
    )


@router.get("/{order_id}", response_model=OrderDetail)
async def get_order(order_id: int, db: Db, session: OrdersReader) -> OrderDetail:
    return await _load_detail(order_id, db, session)


async def _document_row(
    order: Order,
    item: OrderDocumentIn,
    ctx: PricingContext,
    translator: Translator | None,
) -> OrderDocument:
    """A priced document row for `order`, overrides applied."""
    _require_rate(
        ctx, item.source_language, item.target_language, has_price=item.price_override is not None
    )
    breakdown = ctx.price(
        document_type_id=item.document_type_id,
        source_language=item.source_language,
        target_language=item.target_language,
        page_count=item.page_count,
        copy_type=item.copy_type,
        urgency=order.urgency,
        is_notarized=item.is_notarized,
    )
    # Overrides win: the calculated figure is a starting point, and a
    # negotiated price is a business fact.
    translator_cost = item.translator_cost_override
    if translator_cost is None:
        translator_cost = _translator_default_cost(translator, item.page_count)
    if translator_cost is None:
        translator_cost = breakdown.translator_cost

    return OrderDocument(
        order_id=order.id,
        document_type_id=item.document_type_id,
        source_language=item.source_language.lower(),
        target_language=item.target_language.lower(),
        page_count=item.page_count,
        copy_type=item.copy_type,
        is_notarized=breakdown.is_notarized,
        price=item.price_override if item.price_override is not None else breakdown.price,
        translator_cost=translator_cost,
        notary_cost=(
            item.notary_cost_override
            if item.notary_cost_override is not None
            else breakdown.notary_cost
        ),
        translator_id=item.translator_id,
        notary_id=item.notary_id if breakdown.is_notarized else None,
    )


@router.post("", response_model=OrderDetail, status_code=http_status.HTTP_201_CREATED)
async def create_order(payload: OrderCreate, db: Db, session: OrdersWriter) -> OrderDetail:
    client = await db.get(Client, payload.client_id)
    if client is None:
        raise ValidationError("That client does not exist.")

    if payload.handover_method is HandoverMethod.DELIVERY and not payload.delivery_address:
        raise ValidationError("Courier delivery needs a delivery address.")

    type_ids = {d.document_type_id for d in payload.documents}
    # Must match what the quote endpoint showed while the order was built.
    ctx = await load_pricing_context(db, session.plan, type_ids)
    missing = type_ids - set(ctx.multipliers)
    if missing:
        raise ValidationError(f"Unknown document type(s): {sorted(missing)}")

    translators: list[Translator | None] = [
        await _check_assignees(db, session, translator_id=d.translator_id, notary_id=d.notary_id)
        for d in payload.documents
    ]

    order_date = payload.order_date or today_in(session.timezone)
    order = Order(
        client_id=payload.client_id,
        order_date=order_date,
        due_date=payload.due_date or add_days(order_date, ctx.due_days[payload.urgency]),
        contact_info=payload.contact_info or client.phone,
        urgency=payload.urgency,
        handover_method=payload.handover_method,
        delivery_address=payload.delivery_address,
        delivery_cost=(
            ctx.config.delivery_fee
            if payload.handover_method is HandoverMethod.DELIVERY
            else Decimal("0")
        ),
        source=payload.source or "office",
        notes=payload.notes,
        created_by_user_id=session.user_id,
    )
    db.add(order)
    await db.flush()

    for item, translator in zip(payload.documents, translators, strict=True):
        db.add(await _document_row(order, item, ctx, translator))

    db.add(
        OrderStatusEvent(
            order_id=order.id,
            status=INITIAL_STATUS,
            changed_at=datetime.now(UTC),
            changed_by_user_id=session.user_id,
        )
    )
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.created",
        entity_type="order",
        entity_id=order.id,
        after={"client_id": payload.client_id, "documents": len(payload.documents)},
    )

    await notify_everyone(
        db,
        kind=NotificationKind.ORDER_CREATED,
        body=f"New order with {len(payload.documents)} document(s).",
        actor_user_id=session.user_id,
        actor_name=session.full_name or session.username,
        order_id=order.id,
        subject_label=f"{client.name} #{order.id}",
    )
    await db.flush()

    return await _load_detail(order.id, db, session)


def _computed(document: OrderDocument, ctx: PricingContext, urgency: Urgency) -> PriceBreakdown:
    return ctx.price(
        document_type_id=document.document_type_id,
        source_language=document.source_language,
        target_language=document.target_language,
        page_count=document.page_count,
        copy_type=document.copy_type,
        urgency=urgency,
        is_notarized=document.is_notarized,
    )


@router.patch("/{order_id}", response_model=OrderDetail)
async def update_order(
    order_id: int, payload: OrderUpdate, db: Db, session: OrdersWriter
) -> OrderDetail:
    order = await db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found.")

    changes = payload.model_dump(exclude_unset=True)
    for key in ("order_date", "urgency", "handover_method", "delivery_cost"):
        if key in changes and changes[key] is None:
            raise ValidationError(f"{key} cannot be empty.")
    before = {k: getattr(order, k) for k in changes}

    old_urgency = order.urgency
    handover_changed = (
        "handover_method" in changes and changes["handover_method"] != order.handover_method
    )

    for field, value in changes.items():
        setattr(order, field, value)

    if order.handover_method is HandoverMethod.DELIVERY and not order.delivery_address:
        raise ValidationError("Courier delivery needs a delivery address.")

    needs_pricing = handover_changed or order.urgency != old_urgency
    ctx: PricingContext | None = None
    if needs_pricing:
        documents = list(
            (await db.execute(select(OrderDocument).where(OrderDocument.order_id == order.id)))
            .scalars()
            .all()
        )
        ctx = await load_pricing_context(
            db, session.plan, {d.document_type_id for d in documents}
        )

        # The courier fee follows the handover, unless the caller set it.
        if handover_changed and "delivery_cost" not in changes:
            order.delivery_cost = (
                ctx.config.delivery_fee
                if order.handover_method is HandoverMethod.DELIVERY
                else Decimal("0")
            )

        # Urgency is a multiplier on every document's price. Re-price each
        # figure that still equals what the engine computed at the OLD
        # urgency; one that differs was set by hand and is left alone.
        if order.urgency != old_urgency:
            for document in documents:
                was = _computed(document, ctx, old_urgency)
                now = _computed(document, ctx, order.urgency)
                if document.price == was.price:
                    document.price = now.price
                if document.translator_cost == was.translator_cost:
                    document.translator_cost = now.translator_cost
                if document.notary_cost == was.notary_cost:
                    document.notary_cost = now.notary_cost

    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.updated",
        entity_type="order",
        entity_id=order.id,
        before=before,
        after=changes,
    )
    return await _load_detail(order_id, db, session)


@router.post(
    "/{order_id}/documents",
    response_model=OrderDetail,
    status_code=http_status.HTTP_201_CREATED,
)
async def add_order_document(
    order_id: int, payload: OrderDocumentIn, db: Db, session: OrdersWriter
) -> OrderDetail:
    """Add a document to an existing order, priced at the order's urgency."""
    order = await db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found.")

    count = await db.scalar(
        select(func.count()).select_from(OrderDocument).where(OrderDocument.order_id == order_id)
    )
    if (count or 0) >= MAX_DOCUMENTS:
        raise ValidationError(f"An order can have at most {MAX_DOCUMENTS} documents.")

    ctx = await load_pricing_context(db, session.plan, {payload.document_type_id})
    if payload.document_type_id not in ctx.multipliers:
        raise ValidationError("That document type does not exist.")
    translator = await _check_assignees(
        db, session, translator_id=payload.translator_id, notary_id=payload.notary_id
    )

    document = await _document_row(order, payload, ctx, translator)
    db.add(document)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.document_added",
        entity_type="order",
        entity_id=order_id,
        after={"document_id": document.id, "price": str(document.price)},
    )
    return await _load_detail(order_id, db, session)


@router.patch("/{order_id}/documents/{document_id}", response_model=OrderDetail)
async def update_order_document(
    order_id: int,
    document_id: int,
    payload: OrderDocumentUpdate,
    db: Db,
    session: OrdersWriter,
) -> OrderDetail:
    """Change a document: what it is, who does it, what it costs.

    Assigning a translator is what puts a document in their suliko.ge Orders
    tab: the portal shows documents whose ``translator_id`` is a directory row
    linked to that translator's account.
    """
    document = await db.get(OrderDocument, document_id)
    if document is None or document.order_id != order_id:
        raise NotFoundError("Document not found.")
    order = await db.get(Order, order_id)
    if order is None:  # pragma: no cover - the document's FK guarantees it
        raise NotFoundError("Order not found.")

    changes = payload.model_dump(exclude_unset=True)
    nullable = {"translator_id", "notary_id", "is_notarized"}
    for key, value in changes.items():
        if value is None and key not in nullable:
            raise ValidationError(f"{key} cannot be empty.")

    translator = await _check_assignees(
        db,
        session,
        translator_id=changes.get("translator_id"),
        notary_id=changes.get("notary_id"),
    )

    before = {key: getattr(document, key) for key in changes}
    for key in ("source_language", "target_language"):
        if key in changes:
            changes[key] = str(changes[key]).lower()

    structural = {
        "document_type_id",
        "source_language",
        "target_language",
        "page_count",
        "copy_type",
        "is_notarized",
    }
    for key, value in changes.items():
        if key in structural or key in {"translator_id", "notary_id"}:
            setattr(document, key, value)

    if structural & changes.keys():
        # What is being translated changed, so the old figures describe a
        # different job. Re-price, except what was sent explicitly.
        ctx = await load_pricing_context(db, session.plan, {document.document_type_id})
        if document.document_type_id not in ctx.multipliers:
            raise ValidationError("That document type does not exist.")
        _require_rate(
            ctx,
            document.source_language,
            document.target_language,
            has_price="price" in changes,
        )
        if "copy_type" in changes and "is_notarized" not in changes:
            # A new copy type brings its own notarisation; an earlier manual
            # choice described the old one. Any other edit keeps the flag.
            document.is_notarized = document.copy_type.is_notarized
        breakdown = _computed(document, ctx, order.urgency)
        document.is_notarized = breakdown.is_notarized
        document.price = breakdown.price
        assigned = translator
        if assigned is None and document.translator_id is not None:
            assigned = await db.get(Translator, document.translator_id)
        default_cost = _translator_default_cost(assigned, document.page_count)
        document.translator_cost = (
            default_cost if default_cost is not None else breakdown.translator_cost
        )
        document.notary_cost = breakdown.notary_cost
    elif "translator_id" in changes and "translator_cost" not in changes and translator:
        # A new translator with a rate of their own brings that rate.
        default_cost = _translator_default_cost(translator, document.page_count)
        if default_cost is not None:
            document.translator_cost = default_cost

    for key in ("price", "translator_cost", "notary_cost"):
        if key in changes:
            setattr(document, key, changes[key])
    if not document.is_notarized:
        # A notary on a document nobody is notarising is a cost with no job.
        document.notary_id = None
        if "notary_cost" not in changes:
            document.notary_cost = Decimal("0")

    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.document_updated",
        entity_type="order",
        entity_id=order_id,
        before={"document_id": document_id, **before},
        after={"document_id": document_id, **changes},
    )
    return await _load_detail(order_id, db, session)


@router.delete("/{order_id}/documents/{document_id}", response_model=OrderDetail)
async def delete_order_document(
    order_id: int, document_id: int, db: Db, session: OrdersWriter
) -> OrderDetail:
    document = await db.get(OrderDocument, document_id)
    if document is None or document.order_id != order_id:
        raise NotFoundError("Document not found.")

    remaining = await db.scalar(
        select(func.count()).select_from(OrderDocument).where(OrderDocument.order_id == order_id)
    )
    if (remaining or 0) <= 1:
        raise ConflictError(
            "An order needs at least one document. Delete or cancel the order instead."
        )
    notary_paid = await db.scalar(
        select(func.count())
        .select_from(NotaryPaymentAllocation)
        .where(NotaryPaymentAllocation.order_document_id == document_id)
    )
    if notary_paid:
        raise ConflictError(
            "A notary payment is recorded against this document. Remove that payment first."
        )

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.document_removed",
        entity_type="order",
        entity_id=order_id,
        before={
            "document_id": document_id,
            "price": str(document.price),
            "page_count": document.page_count,
        },
    )
    await db.delete(document)
    await db.flush()
    return await _load_detail(order_id, db, session)


@router.post("/{order_id}/status", response_model=OrderDetail)
async def change_status(
    order_id: int,
    payload: StatusChange,
    db: Db,
    session: Annotated[
        AuthenticatedSession, Depends(require(Permission.ORDERS_CHANGE_STATUS))
    ],
) -> OrderDetail:
    """Append a status event. Never updates in place.

    Unknown values are rejected: the vocabulary is closed
    (`domain/statuses.py`), and a typo would otherwise create a status that
    renders as a grey "Unknown" pill forever.
    """
    order = await db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found.")

    if not is_known(payload.status):
        raise ValidationError(f"Unknown status: {payload.status!r}")

    db.add(
        OrderStatusEvent(
            order_id=order_id,
            status=payload.status.strip().lower(),
            changed_at=datetime.now(UTC),
            changed_by_user_id=session.user_id,
            note=payload.note,
        )
    )
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.status_changed",
        entity_type="order",
        entity_id=order_id,
        after={"status": payload.status, "note": payload.note},
    )

    # Only the people already involved with this order, not the whole office:
    # a job moving through six statuses would otherwise generate six entries
    # for everyone.
    involved = (
        (
            await db.execute(
                select(OrderStatusEvent.changed_by_user_id)
                .where(
                    OrderStatusEvent.order_id == order_id,
                    OrderStatusEvent.changed_by_user_id.is_not(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if order.created_by_user_id is not None:
        involved = [*involved, order.created_by_user_id]

    client = await db.get(Client, order.client_id)
    await notify(
        db,
        user_ids=[user_id for user_id in involved if user_id is not None],
        kind=NotificationKind.STATUS_CHANGE,
        body=f"Status changed to {get_label(payload.status)}.",
        actor_user_id=session.user_id,
        actor_name=session.full_name or session.username,
        order_id=order_id,
        subject_label=f"{client.name if client else 'Unknown'} #{order_id}",
    )
    await db.flush()

    return await _load_detail(order_id, db, session)


@router.delete("/{order_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_order(
    order_id: int,
    db: Db,
    session: Annotated[AuthenticatedSession, Depends(require(Permission.ORDERS_DELETE))],
) -> None:
    """Hard delete.

    Cancelling is almost always what is wanted instead — it keeps the history
    and takes the order out of every figure. This exists for genuine mistakes
    (a duplicate, a test order), which is why it needs `orders.delete` rather
    than `orders.write` — and why it refuses once money has been recorded
    against the order. The foreign keys would refuse anyway (payments are
    RESTRICT); checking first is what turns that into a sentence someone can
    act on, and it also covers expenses, which would otherwise CASCADE away
    without a word.
    """
    order = await db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found.")

    money = {
        "client payments": select(func.count())
        .select_from(ClientPaymentAllocation)
        .where(ClientPaymentAllocation.order_id == order_id),
        "translator payouts": select(func.count())
        .select_from(TranslatorPaymentAllocation)
        .where(TranslatorPaymentAllocation.order_id == order_id),
        "notary payments": select(func.count())
        .select_from(NotaryPaymentAllocation)
        .join(
            OrderDocument,
            and_(
                OrderDocument.id == NotaryPaymentAllocation.order_document_id,
                OrderDocument.order_id == order_id,
            ),
        ),
        "expenses": select(func.count()).select_from(Expense).where(Expense.order_id == order_id),
    }
    recorded = [label for label, stmt in money.items() if await db.scalar(stmt)]
    if recorded:
        raise ConflictError(
            f"This order has {', '.join(recorded)} recorded against it, so it cannot be "
            "deleted. Set its status to Cancelled instead — that keeps it out of every "
            "figure and keeps the record."
        )

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.deleted",
        entity_type="order",
        entity_id=order_id,
        before={"client_id": order.client_id, "order_date": str(order.order_date)},
    )
    # Documents and status events cascade.
    await db.delete(order)
    await db.flush()
