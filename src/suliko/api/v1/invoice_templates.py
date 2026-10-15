"""The bureau's own invoice file, and invoices filled into it.

A bureau uploads one of its real invoices as a Word file with ``{{placeholders}}``
(`domain/invoice_template.py`). From then on an order's invoice can be downloaded
as that file, filled in. Without an upload, nothing changes: the invoice screen
keeps Suliko Office's own layout, and this module answers "no template".

Anything the bureau drew in the file (logo, stamp, letterhead) is the file's own,
so a bureau that needs no logo or stamp simply uploads one without. Only the
bureau's owners and admins (``settings.manage``) manage the file; anyone who may
read orders may download an invoice made from it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._files import read_upload
from suliko.api.v1.companies import (
    InvoiceBank,
    InvoiceLine,
    InvoiceOut,
    InvoiceParty,
    build_invoice,
)
from suliko.core.errors import NotFoundError
from suliko.domain.invoice_template import (
    DOCX_TYPE,
    MAX_TEMPLATE_BYTES,
    PLACEHOLDERS,
    check_template,
    fill_template,
    invoice_values,
    unknown_placeholders,
)
from suliko.domain.order_files import safe_file_name
from suliko.models.invoice import InvoiceTemplate
from suliko.security.permissions import Permission

router = APIRouter(tags=["invoice-template"])

Manager = Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))]

#: "Translation services, invoice no. 2610-4F7K": what the client writes on the transfer.
PURPOSE = {
    "ka": "თარგმნის მომსახურება, ინვოისი №",
    "en": "Translation services, invoice no.",
}


class PlaceholderOut(BaseModel):
    name: str
    meaning: str


class TemplateOut(BaseModel):
    """Whether the bureau has uploaded one, and what the file uses."""

    uploaded: bool
    file_name: str | None = None
    size_bytes: int | None = None
    updated_at: datetime | None = None
    #: Names the file uses that Office does not know: they print as nothing.
    unknown: list[str] = []
    #: Everything that can be written, for the list beside the upload.
    placeholders: list[PlaceholderOut]


def _placeholders() -> list[PlaceholderOut]:
    return [PlaceholderOut(name=name, meaning=meaning) for name, meaning in PLACEHOLDERS.items()]


async def _stored(db: Db) -> InvoiceTemplate | None:
    return (await db.execute(select(InvoiceTemplate))).scalars().first()


def _out(row: InvoiceTemplate | None, used: list[str] | None = None) -> TemplateOut:
    if row is None:
        return TemplateOut(uploaded=False, placeholders=_placeholders())
    names = used if used is not None else check_template(row.content)
    return TemplateOut(
        uploaded=True,
        file_name=row.file_name,
        size_bytes=row.size_bytes,
        updated_at=row.updated_at,
        unknown=unknown_placeholders(names),
        placeholders=_placeholders(),
    )


def _docx(content: bytes, name: str) -> Response:
    return Response(
        content=content,
        media_type=DOCX_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{safe_file_name(name)}"'},
    )


@router.get("/invoice-template", response_model=TemplateOut)
async def get_template(db: Db, _: Manager) -> TemplateOut:
    return _out(await _stored(db))


@router.put("/invoice-template", response_model=TemplateOut)
async def upload_template(
    file: Annotated[UploadFile, File()], db: Db, session: CurrentSession, _: Manager
) -> TemplateOut:
    """Keep this Word file as the bureau's invoice. Replaces the one before it."""
    content = await read_upload(file, MAX_TEMPLATE_BYTES + 1)
    used = check_template(content)
    name = safe_file_name(file.filename or "invoice.docx")[:255]
    if not name.lower().endswith(".docx"):
        name = f"{name}.docx"

    row = await _stored(db)
    if row is None:
        row = InvoiceTemplate(
            tenant_id=session.tenant_id, file_name=name, content=content, size_bytes=len(content)
        )
        db.add(row)
    else:
        row.file_name, row.content, row.size_bytes = name, content, len(content)
    await db.flush()
    await db.refresh(row)

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="invoice_template.uploaded",
        entity_type="invoice_template",
        entity_id=row.id,
        after={"file": name, "size": len(content), "unknown": unknown_placeholders(used)},
    )
    return _out(row, used)


@router.delete("/invoice-template", response_model=TemplateOut)
async def remove_template(db: Db, session: CurrentSession, _: Manager) -> TemplateOut:
    """Go back to Suliko Office's own layout."""
    row = await _stored(db)
    if row is not None:
        await db.delete(row)
        await db.flush()

        from suliko.core.audit import record

        await record(
            db,
            session,
            action="invoice_template.removed",
            entity_type="invoice_template",
            entity_id=row.id,
            before={"file": row.file_name},
        )
    return _out(None)


@router.get("/invoice-template/file")
async def download_template(db: Db, _: Manager) -> Response:
    """The file as uploaded."""
    row = await _stored(db)
    if row is None:
        raise NotFoundError("No invoice template has been uploaded.")
    return _docx(row.content, row.file_name)


def sample_invoice(locale: Literal["ka", "en"]) -> InvoiceOut:
    """An invented invoice, to see a template filled in without an order."""
    from datetime import date
    from decimal import Decimal

    return InvoiceOut(
        number="2610-SAMPLE",
        is_provisional=True,
        issued_on=date(2026, 10, 15),
        locale=locale,
        seller=InvoiceParty(
            name="Sample Translations LLC",
            address="12 Rustaveli Ave, Tbilisi",
            id_number="405000000",
            email="office@example.com",
            phone="+995 32 200 00 00",
            site="example.com",
            director="Nino Beridze",
        ),
        seller_brand=None,
        buyer=InvoiceParty(
            name="Giorgi Kapanadze",
            address="5 Chavchavadze Ave, Batumi",
            id_number="01001000000",
            email="giorgi@example.com",
            phone="+995 555 00 00 00",
            site="",
        ),
        bank=InvoiceBank(
            bank_name="Sample Bank",
            iban="GE00XX0000000000000000",
            swift="SAMPGE22",
            account_name="Sample Translations LLC",
        ),
        order_id=0,
        order_number=0,
        reference="T-2026-14",
        order_date=date(2026, 10, 12),
        due_date=date(2026, 10, 26),
        lines=[
            InvoiceLine(
                description="Passport",
                languages="ka → en",
                pages=2,
                unit_price=Decimal("20.00"),
                amount=Decimal("40.00"),
            ),
            InvoiceLine(
                description="Diploma",
                languages="en → ka",
                pages=3,
                unit_price=Decimal("30.00"),
                amount=Decimal("90.00"),
            ),
        ],
        documents_total=Decimal("130.00"),
        delivery_cost=Decimal("10.00"),
        total=Decimal("140.00"),
        paid=Decimal("40.00"),
        balance_due=Decimal("100.00"),
    )


def _filled(content: bytes, invoice: InvoiceOut) -> bytes:
    values, items = invoice_values(invoice, f"{PURPOSE[invoice.locale]} {invoice.number}")
    return fill_template(content, values, items)


@router.get("/invoice-template/sample")
async def sample(db: Db, _: Manager, locale: Literal["ka", "en"] = "ka") -> Response:
    """The uploaded file filled with an invented invoice, to check it before real use."""
    row = await _stored(db)
    if row is None:
        raise NotFoundError("No invoice template has been uploaded.")
    return _docx(_filled(row.content, sample_invoice(locale)), "invoice-sample.docx")


@router.get("/orders/{order_id}/invoice.docx")
async def order_invoice_docx(
    order_id: int,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.ORDERS_READ))],
    locale: Literal["ka", "en"] = "ka",
) -> Response:
    """An order's invoice in the bureau's own file. 404 when there is none:
    the screen then keeps Suliko Office's layout."""
    row = await _stored(db)
    if row is None:
        raise NotFoundError("No invoice template has been uploaded.")
    invoice = await build_invoice(db, session, order_id, locale)
    return _docx(_filled(row.content, invoice), f"invoice-{invoice.number}.docx")
