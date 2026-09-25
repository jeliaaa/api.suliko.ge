"""CSV exports — orders, clients, payments, expenses.

The PHP office exported to Excel constantly (month-end, the accountant, a
client asking for their history), and a SaaS customer's first question about
their data is how to get it out. Each export runs the SAME list endpoint the
screen uses, page by page, so the file matches the filtered view exactly and
there is no second copy of the filter logic to drift.

## Excel and Georgian

The file is UTF-8 with a byte-order mark. Without the BOM, Excel on Windows
opens a CSV as the machine's ANSI code page and every Georgian letter comes out
as mojibake — the single most common complaint about CSV exports here.

## Formula injection

A cell starting with ``=``, ``+``, ``-``, ``@``, tab or CR is prefixed with an
apostrophe. Client names and notes are typed by the public (via the website
and the portal), and Excel would otherwise execute ``=HYPERLINK(...)`` from a
client's name. Numbers are written as numbers and are not touched.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from suliko.api.deps import Db, require
from suliko.api.v1 import clients as clients_api
from suliko.api.v1 import finances as finances_api
from suliko.api.v1 import orders as orders_api
from suliko.domain.clock import today_in
from suliko.models.directory import ClientType
from suliko.models.finance import PaymentMethod
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

router = APIRouter(prefix="/exports", tags=["exports"])

PAGE = 200
#: A ceiling, not a target: a bureau with more rows than this wants a real
#: data export, which is a different feature (on the roadmap).
MAX_ROWS = 50_000

Lang = Literal["ka", "en"]

#: Georgian labels for the values exports spell out. Twins of `statuses.*`
#: and `enums.*` in the frontend's ka.json; `tests/test_exports.py` checks
#: every status and enum value has one.
KA_STATUS: dict[str, str] = {
    "new": "ახალი",
    "confirmed": "დადასტურებული",
    "rejected": "უარყოფილი",
    "payed": "გადახდილი",
    "being_translated": "ითარგმნება",
    "being_corrected": "სწორდება",
    "being_notarised": "ნოტარიულად მოწმდება",
    "in_progress": "მიმდინარე",
    "translated": "ნათარგმნი",
    "translated_by_suliko": "ნათარგმნია სულიკოს მიერ (AI)",
    "sent_for_review": "გაგზავნილია შესამოწმებლად",
    "sent_to_the_translator": "გაგზავნილია თარჯიმანთან",
    "documents_uploaded": "დოკუმენტები ატვირთულია",
    "ready_for_pickup_notary": "მზადაა გასატანად (ნოტარიუსი)",
    "ready_for_pickup_translator": "მზადაა გასატანად (თარჯიმანი)",
    "picked_up": "გატანილია",
    "sent_to_the_client_for_confirmation": "გაგზავნილია კლიენტთან (დასადასტურებლად)",
    "sent_to_the_client": "გაგზავნილია კლიენტთან",
    "sent_to_custom_recipient": "გაგზავნილია სხვა ადრესატთან",
    "reviewed": "შემოწმებული",
    "ready_for_pickup": "მზადაა გასატანად",
    "completed": "დასრულებული",
    "cancelled": "გაუქმებული",
}
KA_CLIENT_TYPE = {"B2B": "იურიდიული პირი", "B2C": "ფიზიკური პირი"}
KA_METHOD = {
    "cash": "ნაღდი",
    "bank_transfer": "საბანკო გადარიცხვა",
    "card": "ბარათი",
    "bog_online": "ონლაინ გადახდა",
    "other": "სხვა",
}
KA_CATEGORY = {
    "courier": "კურიერი",
    "notary_office": "სანოტარო ბიურო",
    "office": "ოფისი",
    "rent": "იჯარა",
    "utilities": "კომუნალური",
    "marketing": "მარკეტინგი",
    "salary": "ხელფასი",
    "software": "პროგრამები",
    "tax": "გადასახადი",
    "other": "სხვა",
}
KA_URGENCY = {"standard": "სტანდარტული", "express": "ექსპრესი", "urgent": "სასწრაფო"}
KA_YES, KA_NO = "კი", "არა"

HEADERS: dict[str, dict[Lang, list[str]]] = {
    "orders": {
        "en": [
            "Order", "Date", "Due", "Client", "Client type", "Status", "Speed", "Languages",
            "Translators", "Documents", "Pages", "Total", "Paid", "Balance", "Overdue",
        ],
        "ka": [
            "შეკვეთა", "თარიღი", "ვადა", "კლიენტი", "კლიენტის ტიპი", "სტატუსი", "სისწრაფე",
            "ენები", "თარჯიმნები", "დოკუმენტები", "გვერდები", "სულ", "გადახდილი", "ნაშთი",
            "ვადაგადაცილებული",
        ],
    },
    "orders_profit": {"en": ["Profit"], "ka": ["მოგება"]},
    "clients": {
        "en": ["ID", "Name", "Type", "Email", "Phone", "ID number"],
        "ka": ["ID", "სახელი", "ტიპი", "ელფოსტა", "ტელეფონი", "პირადი ნომერი"],
    },
    "payments": {
        "en": ["ID", "Date", "Client", "Method", "Amount", "Unallocated", "Orders", "Notes"],
        "ka": [
            "ID", "თარიღი", "კლიენტი", "მეთოდი", "თანხა", "გაუნაწილებელი", "შეკვეთები", "შენიშვნა",
        ],
    },
    "expenses": {
        "en": ["ID", "Date", "Category", "Description", "Order", "Amount"],
        "ka": ["ID", "თარიღი", "კატეგორია", "აღწერა", "შეკვეთა", "თანხა"],
    },
}

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: Any) -> Any:
    """A value ready for a CSV cell, defused against formula injection."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, Decimal)):
        return value
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def csv_response(filename: str, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> Response:
    buffer = io.StringIO()
    buffer.write("﻿")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([safe_cell(cell) for cell in row])
    return Response(
        buffer.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


def _status_label(status: str | None, english: str, lang: Lang) -> str:
    if lang == "en":
        return english
    key = (status or "new").strip().lower().replace(" ", "_")
    return KA_STATUS.get(key, english)


def _label(mapping: dict[str, str], value: str, lang: Lang) -> str:
    return mapping.get(value, value) if lang == "ka" else value


def _stamp(name: str, session: AuthenticatedSession) -> str:
    """`orders-2026-09-25.csv`, dated in the bureau's timezone."""
    return f"{name}-{today_in(session.timezone).isoformat()}.csv"


OrdersReader = Annotated[AuthenticatedSession, Depends(require(Permission.ORDERS_READ))]
ClientsReader = Annotated[AuthenticatedSession, Depends(require(Permission.CLIENTS_READ))]
FinanceReader = Annotated[AuthenticatedSession, Depends(require(Permission.FINANCE_READ))]


@router.get("/orders.csv")
async def export_orders(
    db: Db,
    session: OrdersReader,
    lang: Lang = "ka",
    search: Annotated[str | None, Query(max_length=255)] = None,
    client_type: ClientType | None = None,
    order_status: Annotated[str | None, Query(alias="status", max_length=60)] = None,
    language: Annotated[str | None, Query(max_length=5)] = None,
    client_id: int | None = None,
    translator_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    overdue: bool = False,
    due_today: bool = False,
    unpaid: bool = False,
    mine: bool = False,
    sort: Literal["date", "-date", "id", "-id", "due", "-due"] = "-id",
) -> Response:
    show_profit = orders_api.shows_costs(session)
    header = HEADERS["orders"][lang] + (HEADERS["orders_profit"][lang] if show_profit else [])
    rows: list[list[Any]] = []
    offset = 0
    while offset < MAX_ROWS:
        page = await orders_api.list_orders(
            db=db,
            session=session,
            search=search,
            client_type=client_type,
            order_status=order_status,
            language=language,
            client_id=client_id,
            translator_id=translator_id,
            document_type_id=None,
            date_from=date_from,
            date_to=date_to,
            overdue=overdue,
            due_today=due_today,
            unpaid=unpaid,
            mine=mine,
            sort=sort,
            limit=PAGE,
            offset=offset,
        )
        for order in page.items:
            row: list[Any] = [
                order.id,
                order.order_date,
                order.due_date,
                order.client_name,
                _label(KA_CLIENT_TYPE, order.client_type.value, lang),
                _status_label(order.status, order.status_label, lang),
                _label(KA_URGENCY, order.urgency.value, lang),
                ", ".join(order.language_pairs),
                ", ".join(order.translator_names),
                order.document_count,
                order.page_count,
                order.total,
                order.paid,
                max(order.total - order.paid, Decimal("0.00")),
                (KA_YES if order.is_overdue else KA_NO)
                if lang == "ka"
                else ("yes" if order.is_overdue else "no"),
            ]
            if show_profit:
                row.append(order.profit)
            rows.append(row)
        offset += PAGE
        if offset >= page.meta.total:
            break
    return csv_response(_stamp("orders", session), header, rows)


@router.get("/clients.csv")
async def export_clients(
    db: Db,
    session: ClientsReader,
    lang: Lang = "ka",
    search: Annotated[str | None, Query(max_length=255)] = None,
    client_type: ClientType | None = None,
) -> Response:
    rows: list[list[Any]] = []
    offset = 0
    while offset < MAX_ROWS:
        page = await clients_api.list_clients(
            db,
            session,
            search=search,
            client_type=client_type,
            sort="name",
            limit=PAGE,
            offset=offset,
        )
        for client in page.items:
            rows.append(
                [
                    client.id,
                    client.name,
                    _label(KA_CLIENT_TYPE, client.client_type.value, lang),
                    client.email,
                    client.phone,
                    # Masked, as on the list: an export is not a way around
                    # the audit trail on the full ID number.
                    client.personal_number_masked,
                ]
            )
        offset += PAGE
        if offset >= page.meta.total:
            break
    return csv_response(_stamp("clients", session), HEADERS["clients"][lang], rows)


@router.get("/payments.csv")
async def export_payments(
    db: Db,
    session: FinanceReader,
    lang: Lang = "ka",
    client_id: int | None = None,
    start: date | None = None,
    end: date | None = None,
    method: PaymentMethod | None = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> Response:
    rows: list[list[Any]] = []
    offset = 0
    while offset < MAX_ROWS:
        page = await finances_api.list_payments(
            db,
            session,
            client_id=client_id,
            order_id=None,
            start=start,
            end=end,
            method=method,
            search=search,
            limit=PAGE,
            offset=offset,
        )
        for payment in page.items:
            rows.append(
                [
                    payment.id,
                    payment.payment_date,
                    payment.client_name,
                    _label(KA_METHOD, payment.method.value, lang),
                    payment.amount,
                    payment.unallocated,
                    ", ".join(f"#{a.order_id}" for a in payment.allocations),
                    payment.notes,
                ]
            )
        offset += PAGE
        if offset >= page.meta.total:
            break
    return csv_response(_stamp("payments", session), HEADERS["payments"][lang], rows)


@router.get("/expenses.csv")
async def export_expenses(
    db: Db,
    session: FinanceReader,
    lang: Lang = "ka",
    start: date | None = None,
    end: date | None = None,
    category: Annotated[str | None, Query(max_length=50)] = None,
    search: Annotated[str | None, Query(max_length=255)] = None,
) -> Response:
    rows: list[list[Any]] = []
    offset = 0
    while offset < MAX_ROWS:
        page = await finances_api.list_expenses(
            db,
            session,
            start=start,
            end=end,
            category=category,
            order_id=None,
            search=search,
            sort="-date",
            limit=PAGE,
            offset=offset,
        )
        for expense in page.items:
            rows.append(
                [
                    expense.id,
                    expense.expense_date,
                    _label(KA_CATEGORY, expense.category, lang),
                    expense.description,
                    f"#{expense.order_id}" if expense.order_id else "",
                    expense.amount,
                ]
            )
        offset += PAGE
        if offset >= page.meta.total:
            break
    return csv_response(_stamp("expenses", session), HEADERS["expenses"][lang], rows)
