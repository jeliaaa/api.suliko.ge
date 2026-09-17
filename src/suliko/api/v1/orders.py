"""Orders — the core resource.

The PHP calls these "translations"; they are renamed here because an order is
the commercial object and a translation is one of the things done to it.

Creating an order prices every document through `suliko.domain.pricing` and
stores the result. Costs are STORED, not recomputed on read: a rate change must
never retroactively alter what an existing order charged, and staff routinely
hand-adjust `translator_cost` and `notary_cost` after the fact.

Status is append-only. There is no `status` column — the current status is the
latest row in `order_status_events`. A denormalised column would be a second
source of truth that eventually disagrees with the history.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._shared import PageMeta
from suliko.core.errors import NotFoundError, ValidationError
from suliko.domain.notifications import notify, notify_everyone
from suliko.domain.orders import base_order_query
from suliko.domain.plans import pricing_for_plan
from suliko.domain.pricing import DocumentPricingInput, PricingConfig, price_document
from suliko.domain.statuses import INITIAL_STATUS, get_label, is_known
from suliko.models.collaboration import NotificationKind
from suliko.models.directory import Client, ClientType, Translator
from suliko.models.order import (
    CopyType,
    HandoverMethod,
    Order,
    OrderDocument,
    OrderStatusEvent,
    Urgency,
)
from suliko.models.reference import DocumentType, LanguagePairPrice, TenantSettings
from suliko.security.permissions import Permission

router = APIRouter(prefix="/orders", tags=["orders"])

MAX_DOCUMENTS = 50


# ── Schemas ─────────────────────────────────────────────────────────────────


class OrderDocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_type_id: int
    source_language: str = Field(min_length=2, max_length=5)
    target_language: str = Field(min_length=2, max_length=5)
    page_count: int = Field(ge=1, le=10_000)
    copy_type: CopyType = CopyType.ORIGINAL
    translator_id: int | None = None
    notary_id: int | None = None

    #: Override the computed price. Staff negotiate, and the calculated figure
    #: is a starting point rather than a rule.
    price_override: Decimal | None = Field(default=None, ge=0)
    translator_cost_override: Decimal | None = Field(default=None, ge=0)


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: int
    documents: list[OrderDocumentIn] = Field(min_length=1, max_length=MAX_DOCUMENTS)
    order_date: date | None = None
    due_date: date | None = None
    contact_info: str | None = Field(default=None, max_length=255)
    urgency: Urgency = Urgency.STANDARD
    handover_method: HandoverMethod = HandoverMethod.SCAN
    delivery_address: str | None = Field(default=None, max_length=500)
    source: str | None = Field(default=None, max_length=50)
    notes: str | None = None


class OrderUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    due_date: date | None = None
    contact_info: str | None = Field(default=None, max_length=255)
    urgency: Urgency | None = None
    handover_method: HandoverMethod | None = None
    delivery_address: str | None = Field(default=None, max_length=500)
    notes: str | None = None


class OrderDocumentUpdate(BaseModel):
    """Reassign a document after the order was created.

    Only the fields present are applied, so ``{"translator_id": null}``
    unassigns while ``{}`` changes nothing.
    """

    model_config = ConfigDict(extra="forbid")

    translator_id: int | None = None
    translator_cost: Decimal | None = Field(default=None, ge=0)


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=60)
    note: str | None = Field(default=None, max_length=500)


class OrderDocumentOut(BaseModel):
    id: int
    document_type_id: int
    document_type_name: str | None
    source_language: str
    target_language: str
    page_count: int
    copy_type: CopyType
    is_notarized: bool
    price: Decimal
    translator_cost: Decimal
    notary_cost: Decimal
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
    #: After translator, notary and order expenses.
    profit: Decimal
    #: unpaid | partial | paid — drives the tri-state icon next to Price.
    paid_state: Literal["unpaid", "partial", "paid"]
    is_overdue: bool


class StatusEventOut(BaseModel):
    status: str
    status_label: str
    changed_at: datetime
    changed_by_user_id: int | None
    note: str | None


class OrderDetail(OrderSummary):
    contact_info: str | None
    handover_method: HandoverMethod
    delivery_address: str | None
    delivery_cost: Decimal
    source: str | None
    notes: str | None
    documents_total: Decimal
    translator_total: Decimal
    notary_total: Decimal
    expenses_total: Decimal
    documents: list[OrderDocumentOut]
    status_history: list[StatusEventOut]


class OrderPage(BaseModel):
    items: list[OrderSummary]
    meta: PageMeta


# ── Helpers ─────────────────────────────────────────────────────────────────


def _paid_state(total: Decimal, paid: Decimal) -> Literal["unpaid", "partial", "paid"]:
    """Port of the PHP's `paid_status_icon_html`.

    Compared as Decimal, so a fully-paid order is never reported as partial
    because of float dust.
    """
    if paid <= 0:
        return "unpaid"
    if paid >= total:
        return "paid"
    return "partial"


def _summary_from_row(row: Row[Any]) -> OrderSummary:
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
        # Delivery is revenue with no matching cost, so it belongs in profit.
        profit=gross_profit + order.delivery_cost - expenses,
        paid_state=_paid_state(total, paid),
        is_overdue=bool(order.due_date and order.due_date < datetime.now(UTC).date()),
    )


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("", response_model=OrderPage)
async def list_orders(
    db: Db,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
    search: Annotated[str | None, Query(max_length=255)] = None,
    client_type: ClientType | None = None,
    order_status: Annotated[str | None, Query(alias="status", max_length=60)] = None,
    language: Annotated[str | None, Query(max_length=5)] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    sort: Literal["date", "-date", "id", "-id", "due", "-due"] = "-id",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OrderPage:
    stmt, status_sq, _docs, _paid, _expenses = base_order_query()

    # The client is joined rather than lazy-loaded: the list shows a client
    # name on every row, and lazy loading would be one query per row.
    from sqlalchemy.orm import joinedload

    stmt = stmt.join(Client, Client.id == Order.client_id).options(joinedload(Order.client))

    if search:
        pattern = f"%{search}%"
        conditions: list[ColumnElement[bool]] = [
            Client.name.ilike(pattern),
            Client.email.ilike(pattern),
        ]
        # A bare number is almost always an order id, so match it as one too.
        if search.strip().isdigit():
            conditions.append(Order.id == int(search.strip()))
        stmt = stmt.where(or_(*conditions))

    if client_type is not None:
        stmt = stmt.where(Client.client_type == client_type)

    if order_status:
        stmt = stmt.where(status_sq.c.status == order_status)

    if language:
        # Any document in the order using this language, either direction.
        stmt = stmt.where(
            select(OrderDocument.id)
            .where(
                OrderDocument.order_id == Order.id,
                or_(
                    OrderDocument.source_language == language,
                    OrderDocument.target_language == language,
                ),
            )
            .exists()
        )

    if date_from:
        stmt = stmt.where(Order.order_date >= date_from)
    if date_to:
        stmt = stmt.where(Order.order_date <= date_to)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    column = {
        "date": Order.order_date,
        "due": Order.due_date,
        "id": Order.id,
    }[sort.lstrip("-")]
    stmt = stmt.order_by(column.desc() if sort.startswith("-") else column.asc())

    rows = (await db.execute(stmt.limit(limit).offset(offset))).unique().all()

    return OrderPage(
        items=[_summary_from_row(r) for r in rows],
        meta=PageMeta(total=total, limit=limit, offset=offset),
    )


async def _load_detail(order_id: int, db: AsyncSession) -> OrderDetail:
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

    summary = _summary_from_row(row)
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
                .order_by(OrderStatusEvent.changed_at.desc())
            )
        )
        .scalars()
        .all()
    )

    return OrderDetail(
        **summary.model_dump(),
        contact_info=order.contact_info,
        handover_method=order.handover_method,
        delivery_address=order.delivery_address,
        delivery_cost=order.delivery_cost,
        source=order.source,
        notes=order.notes,
        documents_total=Decimal(row[2] or 0),
        translator_total=Decimal(row[3] or 0),
        notary_total=Decimal(row[4] or 0),
        expenses_total=Decimal(row[9] or 0),
        documents=[
            OrderDocumentOut(
                id=d.id,
                document_type_id=d.document_type_id,
                document_type_name=d.document_type.name_en if d.document_type else None,
                source_language=d.source_language,
                target_language=d.target_language,
                page_count=d.page_count,
                copy_type=d.copy_type,
                is_notarized=d.is_notarized,
                price=d.price,
                translator_cost=d.translator_cost,
                notary_cost=d.notary_cost,
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
                note=e.note,
            )
            for e in history
        ],
    )


@router.get("/{order_id}", response_model=OrderDetail)
async def get_order(
    order_id: int,
    db: Db,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> OrderDetail:
    return await _load_detail(order_id, db)


@router.post("", response_model=OrderDetail, status_code=http_status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> OrderDetail:
    client = await db.get(Client, payload.client_id)
    if client is None:
        raise ValidationError("That client does not exist.")

    if payload.handover_method is HandoverMethod.DELIVERY and not payload.delivery_address:
        raise ValidationError("Courier delivery needs a delivery address.")

    settings = (await db.execute(select(TenantSettings))).scalars().first()
    config = (
        PricingConfig(
            urgency_multipliers={
                Urgency.STANDARD: settings.urgency_multiplier_standard,
                Urgency.EXPRESS: settings.urgency_multiplier_express,
                Urgency.URGENT: settings.urgency_multiplier_urgent,
            },
            delivery_fee=settings.delivery_fee,
            translator_share=settings.default_translator_share,
        )
        if settings
        else PricingConfig.defaults()
    )
    # Must match what the quote endpoint showed while the order was built.
    config = pricing_for_plan(config, session.plan)

    # Load rates and multipliers once rather than per document.
    rates = {
        (r.source_language.lower(), r.target_language.lower()): r.price_per_page
        for r in (
            await db.execute(select(LanguagePairPrice).where(LanguagePairPrice.is_active))
        ).scalars()
    }
    type_ids = {d.document_type_id for d in payload.documents}
    multipliers = {
        t.id: t.price_multiplier
        for t in (
            await db.execute(select(DocumentType).where(DocumentType.id.in_(type_ids)))
        ).scalars()
    }
    missing = type_ids - set(multipliers)
    if missing:
        raise ValidationError(f"Unknown document type(s): {sorted(missing)}")

    delivery_cost = (
        config.delivery_fee if payload.handover_method is HandoverMethod.DELIVERY else Decimal("0")
    )

    order = Order(
        client_id=payload.client_id,
        order_date=payload.order_date or datetime.now(UTC).date(),
        due_date=payload.due_date,
        contact_info=payload.contact_info or client.phone,
        urgency=payload.urgency,
        handover_method=payload.handover_method,
        delivery_address=payload.delivery_address,
        delivery_cost=delivery_cost,
        source=payload.source or "office",
        notes=payload.notes,
        created_by_user_id=session.user_id,
    )
    db.add(order)
    await db.flush()

    for item in payload.documents:
        breakdown = price_document(
            DocumentPricingInput(
                page_count=item.page_count,
                base_rate_per_page=rates.get(
                    (item.source_language.lower(), item.target_language.lower())
                ),
                document_type_multiplier=multipliers[item.document_type_id],
                copy_type=item.copy_type,
                urgency=payload.urgency,
            ),
            config,
        )

        db.add(
            OrderDocument(
                order_id=order.id,
                document_type_id=item.document_type_id,
                source_language=item.source_language.lower(),
                target_language=item.target_language.lower(),
                page_count=item.page_count,
                copy_type=item.copy_type,
                is_notarized=breakdown.is_notarized,
                # Overrides win: the calculated figure is a starting point,
                # and a negotiated price is a business fact.
                price=item.price_override if item.price_override is not None else breakdown.price,
                translator_cost=(
                    item.translator_cost_override
                    if item.translator_cost_override is not None
                    else breakdown.translator_cost
                ),
                notary_cost=breakdown.notary_cost,
                translator_id=item.translator_id,
                notary_id=item.notary_id,
            )
        )

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

    return await _load_detail(order.id, db)


@router.patch("/{order_id}", response_model=OrderDetail)
async def update_order(
    order_id: int,
    payload: OrderUpdate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> OrderDetail:
    order = await db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found.")

    changes = payload.model_dump(exclude_unset=True)
    before = {k: getattr(order, k) for k in changes}

    for field, value in changes.items():
        setattr(order, field, value)

    if order.handover_method is HandoverMethod.DELIVERY and not order.delivery_address:
        raise ValidationError("Courier delivery needs a delivery address.")

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
    return await _load_detail(order_id, db)


@router.patch("/{order_id}/documents/{document_id}", response_model=OrderDetail)
async def update_order_document(
    order_id: int,
    document_id: int,
    payload: OrderDocumentUpdate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> OrderDetail:
    """Assign, reassign or unassign a document's translator.

    This is what puts a document in a translator's suliko.ge Orders tab: the
    portal shows documents whose ``translator_id`` is a directory row linked to
    that translator's account.
    """
    document = await db.get(OrderDocument, document_id)
    if document is None or document.order_id != order_id:
        raise NotFoundError("Document not found.")

    changes = payload.model_dump(exclude_unset=True)
    if "translator_cost" in changes and payload.translator_cost is None:
        raise ValidationError("translator_cost cannot be null.")
    if changes.get("translator_id") is not None:
        translator = await db.get(Translator, payload.translator_id)
        if translator is None:
            raise ValidationError("That translator does not exist.")

    before = {key: getattr(document, key) for key in changes}
    for key, value in changes.items():
        setattr(document, key, value)
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
    return await _load_detail(order_id, db)


@router.post("/{order_id}/status", response_model=OrderDetail)
async def change_status(
    order_id: int,
    payload: StatusChange,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.ORDERS_CHANGE_STATUS))],
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

    return await _load_detail(order_id, db)


@router.delete("/{order_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_order(
    order_id: int,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.ORDERS_DELETE))],
) -> None:
    """Hard delete.

    Cancelling is almost always what is wanted instead — it keeps the history
    and the figures. This exists for genuine mistakes (a duplicate, a test
    order), which is why it needs `orders.delete` rather than `orders.write`.
    """
    order = await db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.deleted",
        entity_type="order",
        entity_id=order_id,
        before={"client_id": order.client_id, "order_date": str(order.order_date)},
    )
    # Documents and status events cascade. Payment allocations are RESTRICT,
    # so an order with money against it refuses to delete — correctly.
    await db.delete(order)
