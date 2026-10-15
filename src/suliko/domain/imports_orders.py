"""Judging the rows of an orders spreadsheet.

One row is one order with one document, priced as typed: an import brings in
history, so the price is never recalculated. The row names its client; the
client is matched to one the bureau already has (phone, email or ID number
first, then the exact name) or created.

A row that is judged ``ok`` may carry a new client. Each new client gets a
placeholder ref (``new:0``, ``new:1``, ...) in the order they first appear, so a
later row for the same person reuses it, and a row that turns out bad never
leaves a client behind.

A row may also name a translator and a notary. They are matched to the bureau's
own (or to ones an earlier sheet of the same import creates) by name, then by
phone. A name nobody knows is left unassigned with a warning, unless the import
has no sheet of translators (``create_translators``) or of notaries
(``create_notaries``) to say who they are: then the person is created, under a ref
(``newt:0``, ``newn:0``) just like a new client, and only once the row is accepted.
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
    RefCounter,
    RowResult,
    Seen,
    Sheet,
    _common,
    cell_text,
    clean_phone,
    parse_date,
    parse_int,
    parse_pairs,
    parse_rate,
    phone_key,
)

OrderKey = tuple[Any, ...]

#: Who a row's translator or notary is: the record's id, or the ref of one this
#: import creates, and its name.
PersonRef = tuple[int | str, str]


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
    #: Translator names (case-folded) to (id or ref, name).
    translators: dict[str, PersonRef] = field(default_factory=dict)
    #: The same by phone (`phone_key`), for a row that gives a phone and a different spelling.
    translators_by_phone: dict[str, PersonRef] = field(default_factory=dict)
    #: Notaries, the same two ways.
    notaries: dict[str, PersonRef] = field(default_factory=dict)
    notaries_by_phone: dict[str, PersonRef] = field(default_factory=dict)
    #: Create a translator or a notary a row names and nobody knows, instead of
    #: leaving the order unassigned.
    create_translators: bool = False
    create_notaries: bool = False
    #: What this import creates that rows point at: ref -> what to save.
    new_translators: dict[str, dict[str, Any]] = field(default_factory=dict)
    new_notaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: One counter per kind of new record for the whole import, so several sheets
    #: share them.
    client_refs: RefCounter = field(default_factory=lambda: RefCounter("new"))
    translator_refs: RefCounter = field(default_factory=lambda: RefCounter("newt"))
    notary_refs: RefCounter = field(default_factory=lambda: RefCounter("newn"))
    #: Orders already known, so a second orders sheet sees the first's rows too.
    #: Filled from `existing_orders` the first time it is needed.
    known_orders: dict[tuple[Any, ...], str] | None = None
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


def remember_person(
    by_name: dict[str, PersonRef],
    by_phone: dict[str, PersonRef],
    ref: int | str | None,
    name: str,
    phone: str | None,
) -> None:
    """Let a row of orders find this person by name or by phone. The first spelling wins."""
    name = name.strip()
    if ref is None or not name:
        return
    person = (ref, name)
    by_name.setdefault(name.casefold(), person)
    if key := phone_key(phone):
        by_phone.setdefault(key, person)


def _find_person(
    by_name: dict[str, PersonRef], by_phone: dict[str, PersonRef], name: str, phone: str | None
) -> PersonRef | None:
    """By name first, then by phone: the same person is not always spelled the same."""
    if name and (found := by_name.get(name.casefold())):
        return found
    key = phone_key(phone)
    return by_phone.get(key) if key else None


def _create_person(
    created: dict[str, dict[str, Any]],
    by_name: dict[str, PersonRef],
    by_phone: dict[str, PersonRef],
    refs: RefCounter,
    name: str,
    phone: str | None,
) -> PersonRef:
    """A person the sheet names and nobody knows: they get a ref, and later rows find them."""
    ref = next(refs)
    created[ref] = {"name": name[:255], "phone": phone, "pairs": []}
    person = (ref, name)
    by_name[name.casefold()] = person
    if key := phone_key(phone):
        by_phone[key] = person
    return person


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
    if ctx.known_orders is None:
        ctx.known_orders = {
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
    known = ctx.known_orders
    return [_judge_order(number, cells, mapping, seen, ctx, known) for number, cells in sheet.rows]


def _judge_order(
    number: int,
    cells: list[Any],
    mapping: dict[str, int],
    seen: Seen,
    ctx: OrdersContext,
    known: dict[OrderKey, str],
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
    translator_phone = clean_phone(_text(cells, mapping, "translator_phone"))
    translator = _find_person(
        ctx.translators, ctx.translators_by_phone, translator_text, translator_phone
    )
    new_translator = bool(translator_text) and translator is None and ctx.create_translators
    if translator_text and translator is None and not ctx.create_translators:
        result.warnings.append(f"Translator {translator_text[:30]!r} not found: left unassigned.")

    cost_text = _text(cells, mapping, "translator_cost")
    translator_cost = parse_rate(cost_text) if cost_text else None
    if cost_text and translator_cost is None:
        result.warnings.append(f"Translator cost left out: {cost_text[:20]!r} is not an amount.")

    notary_text = _text(cells, mapping, "notary")
    notary_phone = clean_phone(_text(cells, mapping, "notary_phone"))
    notary = _find_person(ctx.notaries, ctx.notaries_by_phone, notary_text, notary_phone)
    new_notary = bool(notary_text) and notary is None and ctx.create_notaries
    if notary_text and notary is None and not ctx.create_notaries:
        result.warnings.append(f"Notary {notary_text[:30]!r} not found: left unassigned.")

    notary_cost_text = _text(cells, mapping, "notary_cost")
    notary_cost = parse_rate(notary_cost_text) if notary_cost_text else None
    if notary_cost_text and notary_cost is None:
        result.warnings.append(f"Notary cost left out: {notary_cost_text[:20]!r} is not an amount.")

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
        client_ref = ctx.client_refs.peek()
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
    if new_translator:
        translator = _create_person(
            ctx.new_translators,
            ctx.translators,
            ctx.translators_by_phone,
            ctx.translator_refs,
            translator_text,
            translator_phone,
        )
    if new_notary:
        notary = _create_person(
            ctx.new_notaries,
            ctx.notaries,
            ctx.notaries_by_phone,
            ctx.notary_refs,
            notary_text,
            notary_phone,
        )
    if translator is not None and translator[0] in ctx.new_translators:
        # A translator made from the sheet works the pairs the sheet shows them in.
        pairs = ctx.new_translators[str(translator[0])]["pairs"]
        if (source, target) not in pairs:
            pairs.append((source, target))
    if new_client is not None:
        next(ctx.client_refs)
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
        "notary_id": notary[0] if notary else None,
        "notary_cost": notary_cost or Decimal("0"),
        "status": status,
        "paid": paid,
        "identifier": identifier,
        "notes": notes,
    }
    return result
