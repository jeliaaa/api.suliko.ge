"""A bureau's own invoice, as a Word file with placeholders.

The bureau uploads one of its real invoices with ``{{placeholders}}`` where the
order's details go; Office fills them in and returns a Word file. Everything the
bureau drew (its logo, stamp, letterhead, fonts, borders) stays exactly as in the
upload, because only the text of the placeholders is touched.

## Placeholders

Written ``{{name}}``, case and inner spaces do not matter. See ``PLACEHOLDERS``
for the list; ``{{client}}`` and ``{{total}}`` are the ones most files start with.

**Lines.** A table row that holds a ``{{item.*}}`` placeholder is the pattern for
one document: it is repeated once per line of the invoice, in place. A
paragraph that is just ``{{items}}`` is replaced by one plain line per document,
for files that have no table.

**Unknown names** are replaced by nothing, and the upload says which they were,
so a typo shows up when the file is uploaded and not on a client's invoice.

A placeholder split across formatting runs by Word (``{{to`` bold, ``tal}}``
plain) is still found: its paragraph's runs are merged into the first one. A
placeholder that sits whole inside one run keeps that run's formatting.
"""

from __future__ import annotations

import copy
import io
import re
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from docx import Document
from docx.document import Document as DocxDocument
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from suliko.core.errors import ValidationError

#: What one uploaded template may weigh. A letterhead with a logo is far below it.
MAX_TEMPLATE_BYTES = 2 * 1024 * 1024

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: Placeholder -> what it is, for the list shown beside the upload. Order is display order.
PLACEHOLDERS: dict[str, str] = {
    "number": "Invoice number",
    "issued_on": "Date issued",
    "order_date": "Order date",
    "due_date": "Due date (empty when none)",
    "reference": "The client's own reference, such as a tender number",
    "seller_name": "Your company's name",
    "seller_brand": "Your trading name, when it differs",
    "seller_address": "Your address",
    "seller_id": "Your registration number",
    "seller_email": "Your email",
    "seller_phone": "Your phone",
    "seller_site": "Your website",
    "seller_director": "Who signs for you",
    "client": "The client's name",
    "buyer_name": "The client's name (same as client)",
    "buyer_address": "The client's address",
    "buyer_id": "The client's ID or registration number",
    "buyer_email": "The client's email",
    "buyer_phone": "The client's phone",
    "bank_name": "Bank",
    "iban": "IBAN",
    "swift": "SWIFT",
    "account_name": "Account holder",
    "purpose": "Payment purpose to write on the transfer",
    "documents_total": "Total of the documents",
    "delivery": "Delivery cost",
    "total": "Total",
    "paid": "Already paid",
    "balance_due": "Still to pay",
    "items": "One line per document, for a file with no table",
    "item.n": "In a table row: the line's number",
    "item.description": "In a table row: the document",
    "item.languages": "In a table row: the languages",
    "item.quantity": "In a table row: pages, minutes or characters",
    "item.unit_price": "In a table row: price per unit",
    "item.amount": "In a table row: the line's amount",
}

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")


def _name(raw: str) -> str:
    return raw.strip().lower()


# ── Walking a document ──────────────────────────────────────────────────────


def _cells_paragraphs(table: Table) -> Iterator[Paragraph]:
    for row in table.rows:
        seen: set[int] = set()
        for cell in row.cells:
            if id(cell._tc) in seen:  # a merged cell appears once per column it spans
                continue
            seen.add(id(cell._tc))
            yield from _container_paragraphs(cell)


def _container_paragraphs(container: DocxDocument | _Cell | Any) -> Iterator[Paragraph]:
    yield from container.paragraphs
    for table in container.tables:
        yield from _cells_paragraphs(table)


def _all_paragraphs(document: DocxDocument) -> Iterator[Paragraph]:
    """Body, tables (nested too), and every header and footer."""
    yield from _container_paragraphs(document)
    for section in document.sections:
        for part in (
            section.header,
            section.first_page_header,
            section.even_page_header,
            section.footer,
            section.first_page_footer,
            section.even_page_footer,
        ):
            yield from _container_paragraphs(part)


def _all_tables(container: Any) -> Iterator[Table]:
    for table in container.tables:
        yield table
        for row in table.rows:
            for cell in row.cells:
                yield from _all_tables(cell)


def _open(content: bytes) -> DocxDocument:
    try:
        return Document(io.BytesIO(content))
    except Exception as exc:  # python-docx raises several kinds on a bad file
        raise ValidationError("That file is not a Word document (.docx).") from exc


def check_template(content: bytes) -> list[str]:
    """Refuse a file that is not a usable .docx; the placeholders it uses otherwise."""
    if not content:
        raise ValidationError("The file is empty.")
    if len(content) > MAX_TEMPLATE_BYTES:
        raise ValidationError(f"The file is larger than {MAX_TEMPLATE_BYTES // (1024 * 1024)} MB.")
    document = _open(content)
    found: list[str] = []
    for paragraph in _all_paragraphs(document):
        for match in _PLACEHOLDER.finditer(paragraph.text):
            name = _name(match.group(1))
            if name not in found:
                found.append(name)
    return found


def unknown_placeholders(names: Sequence[str]) -> list[str]:
    return [name for name in names if name not in PLACEHOLDERS]


# ── Filling ─────────────────────────────────────────────────────────────────


def _fill_paragraph(paragraph: Paragraph, values: Mapping[str, str]) -> None:
    if "{{" not in paragraph.text:
        return

    def sub(match: re.Match[str]) -> str:
        return values.get(_name(match.group(1)), "")

    # First, placeholders that sit whole inside one run keep that run's look.
    for run in paragraph.runs:
        if "{{" in run.text:
            run.text = _PLACEHOLDER.sub(sub, run.text)
    # Then any Word split across runs: merge into the first run.
    if "{{" in paragraph.text and paragraph.runs:
        merged = _PLACEHOLDER.sub(sub, paragraph.text)
        first, *rest = paragraph.runs
        first.text = merged
        for run in rest:
            run._r.getparent().remove(run._r)


def _is_item_row(row: Any) -> bool:
    return any("{{item." in cell.text.replace(" ", "").lower() for cell in row.cells)


def _fill_item_rows(document: DocxDocument, items: Sequence[Mapping[str, str]]) -> None:
    for table in list(_all_tables(document)):
        for row in list(table.rows):
            if not _is_item_row(row):
                continue
            anchor = row._tr
            for index, item in enumerate(items):
                copy_tr = copy.deepcopy(anchor)
                anchor.addprevious(copy_tr)
                new_row = next(r for r in table.rows if r._tr is copy_tr)
                values = {f"item.{key}": value for key, value in item.items()}
                values["item.n"] = str(index + 1)
                for cell in new_row.cells:
                    for paragraph in cell.paragraphs:
                        _fill_paragraph(paragraph, values)
            anchor.getparent().remove(anchor)


def _fill_items_paragraphs(document: DocxDocument, items: Sequence[Mapping[str, str]]) -> None:
    for paragraph in list(_all_paragraphs(document)):
        if paragraph.text.replace(" ", "").lower() != "{{items}}":
            continue
        lines = [
            f"{item['description']}, {item['languages']}: {item['quantity']} × "  # noqa: RUF001
            f"{item['unit_price']} = {item['amount']}"
            for item in items
        ]
        anchor = paragraph._p
        for text in lines[1:]:
            clone = copy.deepcopy(anchor)
            anchor.addnext(clone)
            Paragraph(clone, paragraph._parent).runs[0].text = text
        if paragraph.runs:
            first, *rest = paragraph.runs
            first.text = lines[0] if lines else ""
            for run in rest:
                run._r.getparent().remove(run._r)


def fill_template(
    content: bytes, values: Mapping[str, str], items: Sequence[Mapping[str, str]]
) -> bytes:
    """The template with its placeholders replaced. ``values`` is keyed by lower-case name."""
    document = _open(content)
    _fill_item_rows(document, items)
    _fill_items_paragraphs(document, items)
    for paragraph in _all_paragraphs(document):
        _fill_paragraph(paragraph, values)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


# ── What goes in ────────────────────────────────────────────────────────────


def _money(value: Decimal) -> str:
    return f"{value:,.2f} ₾"  # noqa: RUF001


def _date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


_UNITS = {
    "ka": {"minutes": "წთ", "characters": "სიმბ."},
    "en": {"minutes": "min", "characters": "chars"},
}


def invoice_values(invoice: Any, purpose: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    """The placeholders and the lines for an invoice (the API's ``InvoiceOut``)."""
    seller, buyer, bank = invoice.seller, invoice.buyer, invoice.bank
    values = {
        "number": invoice.number,
        "issued_on": _date(invoice.issued_on),
        "order_date": _date(invoice.order_date),
        "due_date": _date(invoice.due_date),
        "reference": invoice.reference or "",
        "seller_name": seller.name,
        "seller_brand": invoice.seller_brand or "",
        "seller_address": seller.address,
        "seller_id": seller.id_number,
        "seller_email": seller.email,
        "seller_phone": seller.phone,
        "seller_site": seller.site,
        "seller_director": seller.director,
        "client": buyer.name,
        "buyer_name": buyer.name,
        "buyer_address": buyer.address,
        "buyer_id": buyer.id_number,
        "buyer_email": buyer.email,
        "buyer_phone": buyer.phone,
        "bank_name": bank.bank_name if bank else "",
        "iban": bank.iban if bank else "",
        "swift": bank.swift if bank else "",
        "account_name": bank.account_name if bank else "",
        "purpose": purpose,
        "documents_total": _money(invoice.documents_total),
        "delivery": _money(invoice.delivery_cost),
        "total": _money(invoice.total),
        "paid": _money(invoice.paid),
        "balance_due": _money(invoice.balance_due),
    }
    units = _UNITS.get(invoice.locale, _UNITS["en"])
    items = []
    for line in invoice.lines:
        quantity = f"{line.pages:,}".replace(",", " ")
        if line.unit != "pages":
            quantity = f"{quantity} {units[line.unit]}"
        items.append(
            {
                "description": line.description,
                "languages": line.languages,
                "quantity": quantity,
                "unit_price": _money(line.unit_price),
                "amount": _money(line.amount),
            }
        )
    return values, items
