"""Judging the rows of an orders spreadsheet.

One row is one order with one document, priced as typed: an import brings in
history, so the price is never recalculated. The row names its client; the
client is matched to one the bureau already has (phone, email or ID number
first, then the exact name) or created.

A row that is judged ``ok`` may carry a new client. Each new client gets a
placeholder ref (``new:0``, ``new:1``, ...) in the order they first appear, so a
later row for the same person reuses it, and a row that turns out bad never
leaves a client behind.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from suliko.domain.imports import (
    _COMPANY_MARKS,
    ExistingPerson,
    RowResult,
    Seen,
    Sheet,
    _common,
    cell_text,
    parse_date,
    parse_int,
    parse_pairs,
    parse_rate,
)

OrderKey = tuple[Any, ...]


@dataclass
class ExistingOrder:
    """An order the bureau already has, to recognise a row that is the same order."""

    label: str
    client_id: int
    order_date: date
    source_language: str
    target_language: str
    page_count: int
    price: Decimal
    identifier: str | None = None


@dataclass
class OrdersContext:
    #: A code or a name (any language) to the bureau's language code.
    language_lookup: Callable[[str], str | None]
    #: Document type names (English and Georgian, case-folded) to (id, label).
    document_types: dict[str, tuple[int, str]] = field(default_factory=dict)
    #: Used, with a warning, when the sheet gives no type or one not recognised.
    default_document_type: tuple[int, str] | None = None
    #: Translator names (case-folded) to (id, name).
    translators: dict[str, tuple[int, str]] = field(default_factory=dict)
    #: A status as typed (a stored key, a label, one of the bureau's own) to the
    #: value to store, or None.
    status_lookup: Callable[[str], str | None] = lambda _text: None
    existing_orders: list[ExistingOrder] = field(default_factory=list)


def order_key(
    client: int | str,
    order_date: date,
    source: str,
    target: str,
    pages: int,
    price: Decimal,
    identifier: str | None,
) -> OrderKey:
    return (client, order_date, source, target, pages, price, (identifier or "").casefold())


def _raw(cells: list[Any], mapping: dict[str, int], key: str) -> Any:
    index = mapping.get(key)
    if index is None or index >= len(cells):
        return None
    return cells[index]


def _text(cells: list[Any], mapping: dict[str, int], key: str) -> str:
    return cell_text(_raw(cells, mapping, key))


def judge_orders(
    sheet: Sheet, mapping: dict[str, int], seen: Seen, ctx: OrdersContext
) -> list[RowResult]:
    known: dict[OrderKey, str] = {
        order_key(
            o.client_id,
            o.order_date,
            o.source_language,
            o.target_language,
            o.page_count,
            o.price,
            o.identifier,
        ): o.label
        for o in ctx.existing_orders
    }
    new_clients = [0]
    return [
        _judge_order(number, cells, mapping, seen, ctx, known, new_clients)
        for number, cells in sheet.rows
    ]


def _judge_order(
    number: int,
    cells: list[Any],
    mapping: dict[str, int],
    seen: Seen,
    ctx: OrdersContext,
    known: dict[OrderKey, str],
    new_clients: list[int],
) -> RowResult:
    result = RowResult(row=number, status="ok")

    def fail(message: str) -> RowResult:
        result.status = "error"
        result.messages.append(message)
        return result

    # ── The order ───────────────────────────────────────────────────────────
    order_date = parse_date(_raw(cells, mapping, "order_date"))
    if order_date is None:
        shown = _text(cells, mapping, "order_date")
        return fail(
            f"The order date {shown!r} is not a date (write it as 25.10.2025)."
            if shown
            else "No order date."
        )

    due_raw = _raw(cells, mapping, "due_date")
    due_date = parse_date(due_raw)
    if cell_text(due_raw) and due_date is None:
        result.warnings.append(f"Due date left out: {cell_text(due_raw)[:20]!r} is not a date.")

    pair_text = _text(cells, mapping, "language_pair")
    if pair_text:
        pairs, _problems = parse_pairs(pair_text, ctx.language_lookup)
        source, target = pairs[0] if pairs else (None, None)
    else:
        source = ctx.language_lookup(_text(cells, mapping, "source_language") or "\0")
        target = ctx.language_lookup(_text(cells, mapping, "target_language") or "\0")
    if not source or not target or source == target:
        return fail(
            "The languages are not recognised (use ka-en, or two columns with the "
            "language names or codes)."
        )

    type_text = _text(cells, mapping, "document_type")
    document_type = ctx.document_types.get(type_text.casefold()) if type_text else None
    if document_type is None:
        if ctx.default_document_type is None:
            return fail("The bureau has no document type to use. Add one in Settings.")
        document_type = ctx.default_document_type
        if type_text:
            result.warnings.append(
                f"Document type {type_text[:30]!r} not recognised: used {document_type[1]}."
            )

    pages_text = _text(cells, mapping, "pages")
    pages = parse_int(pages_text) if pages_text else 1
    if pages is None or not 1 <= pages <= 10_000:
        result.warnings.append(f"Pages {pages_text[:10]!r} not understood: used 1.")
        pages = 1

    price_text = _text(cells, mapping, "price")
    price = parse_rate(price_text) if price_text else None
    if price is None:
        return fail(f"The price {price_text!r} is not an amount." if price_text else "No price.")

    translator_text = _text(cells, mapping, "translator")
    translator = ctx.translators.get(translator_text.casefold()) if translator_text else None
    if translator_text and translator is None:
        result.warnings.append(f"Translator {translator_text[:30]!r} not found: left unassigned.")

    cost_text = _text(cells, mapping, "translator_cost")
    translator_cost = parse_rate(cost_text) if cost_text else None
    if cost_text and translator_cost is None:
        result.warnings.append(f"Translator cost left out: {cost_text[:20]!r} is not an amount.")

    status_text = _text(cells, mapping, "status")
    status = ctx.status_lookup(status_text) if status_text else None
    if status_text and status is None:
        result.warnings.append(f"Status {status_text[:30]!r} not recognised: the order is New.")

    paid_text = _text(cells, mapping, "paid")
    paid = parse_rate(paid_text) if paid_text else None
    if paid_text and paid is None:
        result.warnings.append(f"Paid amount left out: {paid_text[:20]!r} is not an amount.")
    if paid is not None and paid > price:
        result.warnings.append(f"Paid {paid} is more than the price: recorded as {price}.")
        paid = price
    if paid is not None and paid <= 0:
        paid = None

    identifier = _text(cells, mapping, "identifier")[:100] or None
    notes = _text(cells, mapping, "notes") or None

    # ── The client ──────────────────────────────────────────────────────────
    name_text = _text(cells, mapping, "client_name")
    # `_common` reads "name", "phone" and "email"; this sheet calls them client_*.
    shim = {
        field: mapping[column]
        for field, column in (
            ("name", "client_name"),
            ("phone", "client_phone"),
            ("email", "client_email"),
        )
        if column in mapping
    }
    name, phone, email = _common(cells, shim, result)
    name = name or name_text
    personal = _text(cells, mapping, "client_personal_number")[:50] or None

    person = ExistingPerson(
        label=name, phone=phone, email=email, personal_number=personal, name=name or None
    )
    hit = seen.match_ref(person, by_name=bool(name))
    new_client: dict[str, Any] | None = None
    if hit is not None:
        client_ref = hit[2]
        client_label = hit[1]
        if hit[0] == "name":
            result.warnings.append(
                f"Client matched by name only: {client_label}. Check it is the same person."
            )
    else:
        if not name:
            return fail("No client name, and no phone, email or ID number that matches a client.")
        if len(name) > 255:
            return fail("The client name is longer than 255 characters.")
        client_ref = f"new:{new_clients[0]}"
        client_label = name
        new_client = {
            "name": name,
            "client_type": "B2B" if _COMPANY_MARKS.search(name) else "B2C",
            "phone": phone,
            "email": email,
            "personal_number": personal,
        }
    result.name = client_label

    # ── The same order again? ───────────────────────────────────────────────
    key = order_key(client_ref, order_date, source, target, pages, price, identifier)
    if key in known:
        result.status = "duplicate"
        result.matched = known[key]
        result.messages.append(f"Looks like an order already there: {known[key]}.")
        return result

    # ── Accepted: from here the row is real ─────────────────────────────────
    known[key] = f"row {number}"
    if new_client is not None:
        new_clients[0] += 1
        seen.add(
            ExistingPerson(
                label=name,
                phone=phone,
                email=email,
                personal_number=personal,
                name=name,
                ref=client_ref,
            )
        )
    result.values = {
        "client": {"ref": client_ref, "new": new_client},
        "order_date": order_date,
        "due_date": due_date,
        "source_language": source,
        "target_language": target,
        "document_type_id": document_type[0],
        "document_type_label": document_type[1],
        "page_count": pages,
        "price": price,
        "translator_id": translator[0] if translator else None,
        "translator_cost": translator_cost or Decimal("0"),
        "status": status,
        "paid": paid,
        "identifier": identifier,
        "notes": notes,
    }
    return result
