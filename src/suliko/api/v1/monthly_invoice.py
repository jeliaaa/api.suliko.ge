"""A client's monthly invoice: every order delivered to them in one month.

Built on demand from the orders' STORED amounts, like the order invoice
(``companies.build_invoice``), and not kept: there is no table behind it, so
nothing here can drift from the orders. The number is derived (month and the
client's number), so it is the same on every view and can go in a bank
transfer's purpose.

An order belongs to the month it was moved to "Delivered" (the status event's
date in the bureau's time zone), not the month it was placed. A cancelled or
rejected order never counts.

Lines are grouped by language pair: how many orders and pages for each. The
notary fee is already inside each document's price, so the totals split the
same sum in two (translation, notary) and the grand total does not change.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1.companies import (
    InvoiceBank,
    InvoiceParty,
    _issuer,
    _language_pair,
    _party,
    _unit_of,
)
from suliko.core.errors import NotFoundError
from suliko.domain.clock import today_in, zone
from suliko.domain.orders import base_order_query
from suliko.domain.statuses import EXCLUDED_FROM_AGGREGATES, sql_values
from suliko.models.directory import Client
from suliko.models.order import Order, OrderDocument, OrderStatusEvent
from suliko.models.reference import Language
from suliko.security.permissions import Permission

router = APIRouter(prefix="/clients", tags=["companies"])


class MonthlyLine(BaseModel):
    #: "ქართული → ინგლისური", in the invoice's language.
    languages: str
    #: Pages, minutes or characters, as `unit` says.
    unit: Literal["pages", "minutes", "characters"]
    orders: int
    quantity: int
    #: The translation part only: the notary fee is shown in the totals.
    amount: Decimal


class MonthlyInvoiceOut(BaseModel):
    number: str
    issued_on: date
    locale: Literal["ka", "en"]
    #: "2026-10".
    month: str
    seller: InvoiceParty
    seller_brand: str | None
    buyer: InvoiceParty
    bank: InvoiceBank
    order_count: int
    lines: list[MonthlyLine]
    translation_total: Decimal
    notary_total: Decimal
    delivery_cost: Decimal
    total: Decimal
    paid: Decimal
    balance_due: Decimal
    currency: str = "GEL"


@dataclass
class _Group:
    orders: set[int] = field(default_factory=set)
    quantity: int = 0
    amount: Decimal = Decimal(0)


def _month_bounds(month: str, tz_name: str | None) -> tuple[datetime, datetime]:
    """The month's first instant and the next month's, in UTC, for the bureau's
    own calendar (an order delivered at 01:00 on the 1st belongs to that month,
    not to the UTC date before it)."""
    year, mon = int(month[:4]), int(month[5:7])
    tz = zone(tz_name)
    start = datetime(year, mon, 1, tzinfo=tz)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


@router.get("/{client_id}/monthly-invoice", response_model=MonthlyInvoiceOut)
async def monthly_invoice(
    client_id: int,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.ORDERS_READ))],
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    locale: Literal["ka", "en"] = "ka",
) -> MonthlyInvoiceOut:
    client = await db.get(Client, client_id)
    if client is None:
        raise NotFoundError("Client not found.")

    legal, brand, primary = await _issuer(db)
    start, end = _month_bounds(month, session.timezone)

    # When each order was (last) moved to Delivered.
    delivered = (
        select(
            OrderStatusEvent.order_id.label("order_id"),
            func.max(OrderStatusEvent.changed_at).label("at"),
        )
        .where(OrderStatusEvent.status == "completed")
        .group_by(OrderStatusEvent.order_id)
        .subquery("delivered")
    )
    stmt, status_sq, _docs, _paid, _exp = base_order_query()
    stmt = (
        stmt.join(delivered, delivered.c.order_id == Order.id)
        .where(
            Order.client_id == client_id,
            delivered.c.at >= start,
            delivered.c.at < end,
            status_sq.c.status.not_in(sql_values(EXCLUDED_FROM_AGGREGATES)),
        )
        .order_by(delivered.c.at, Order.id)
    )
    rows = (await db.execute(stmt)).unique().all()

    order_ids = [row[0].id for row in rows]
    delivery_cost = sum((row[0].delivery_cost for row in rows), Decimal(0))
    paid = sum((Decimal(row[8] or 0) for row in rows), Decimal(0))

    docs: list[OrderDocument] = []
    if order_ids:
        docs = list(
            (
                await db.execute(
                    select(OrderDocument)
                    .where(OrderDocument.order_id.in_(order_ids))
                    .options(joinedload(OrderDocument.document_type))
                    .order_by(OrderDocument.order_id, OrderDocument.id)
                )
            )
            .unique()
            .scalars()
            .all()
        )

    codes = {d.source_language for d in docs} | {d.target_language for d in docs}
    names = {
        language.code: language
        for language in (
            await db.execute(select(Language).where(Language.code.in_(codes)))
        ).scalars()
    }

    groups: dict[tuple[str, str], _Group] = defaultdict(_Group)
    notary_total = Decimal(0)
    for d in docs:
        unit = _unit_of(d)
        group = groups[(_language_pair(d.source_language, d.target_language, names, locale), unit)]
        group.orders.add(d.order_id)
        group.quantity += (d.character_count if unit == "characters" else d.page_count) or 0
        group.amount += d.price - d.notary_cost
        notary_total += d.notary_cost

    lines = [
        MonthlyLine(
            languages=languages,
            unit=unit,  # type: ignore[arg-type]
            orders=len(group.orders),
            quantity=group.quantity,
            amount=group.amount,
        )
        for (languages, unit), group in sorted(groups.items())
    ]

    translation_total = sum((line.amount for line in lines), Decimal(0))
    total = translation_total + notary_total + delivery_cost

    seller = _party(legal, locale)

    return MonthlyInvoiceOut(
        number=f"M{month[2:4]}{month[5:7]}-{client.number}",
        issued_on=today_in(session.timezone),
        locale=locale,
        month=month,
        seller=seller,
        seller_brand=(
            _party(brand, locale).name
            if brand and _party(brand, locale).name != seller.name
            else None
        ),
        buyer=InvoiceParty(
            name=client.name,
            address=client.address or "",
            id_number=client.personal_number or "",
            email=client.email or "",
            phone=client.phone or "",
            site="",
        ),
        bank=InvoiceBank(
            bank_name=primary.bank_name,
            iban=primary.bank_iban,
            swift=primary.bank_swift,
            account_name=primary.account_name,
        ),
        order_count=len(order_ids),
        lines=lines,
        translation_total=translation_total,
        notary_total=notary_total,
        delivery_cost=delivery_cost,
        total=total,
        paid=paid,
        balance_due=total - paid,
    )
