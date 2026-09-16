"""The tenant's own legal identity, and the invoices it issues.

The PHP keeps exactly two global company rows — `legal_entity` and `brand` —
because it serves one bureau. Here they are per tenant, which is the whole
point of the B2B pivot: each partner bureau invoices under its own name,
registration number and bank account.

## Why two companies and not one

`legal_entity` is who the money is owed to: the name on the invoice, the tax
ID, the bank account. `brand` is who the client thinks they are dealing with:
the trading name, the website, the phone number on the footer. They are
frequently different, and an invoice that shows the brand where the law wants
the legal entity is not a valid invoice.

Both are optional. A bureau that has not filled them in can still run orders;
it just cannot issue an invoice, and `GET /companies/invoice-readiness` says
exactly what is missing rather than letting them find out at the point of
sending one to a client.

## Invoices are computed, never stored

`GET /orders/{id}/invoice` assembles an invoice from the order's STORED
document costs plus the company record as it is right now. Nothing is written.

That is deliberate and it has a consequence worth knowing: reissuing an
invoice after the company's address changes produces the new address. The
figures cannot drift — those are stored on the documents and never recomputed
— but the letterhead can. Freezing the whole document would mean a
`generated_invoices` table, a numbering sequence and an immutability rule,
which is the right design once invoices are legally issued rather than
informally sent. It is not built yet, and the numbering below says so.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._shared import mask_tail
from suliko.core.errors import ConflictError, NotFoundError
from suliko.domain.orders import base_order_query
from suliko.models.directory import Client
from suliko.models.order import Order, OrderDocument
from suliko.models.reference import Company, CompanyBankAccount
from suliko.security.permissions import Permission

router = APIRouter(prefix="/companies", tags=["companies"])

CompanyRole = Literal["legal_entity", "brand"]

#: What an invoice cannot be issued without. Everything else on the company is
#: presentational; these are the fields that make it a document rather than a
#: letter.
INVOICE_REQUIRED = ("name_ka", "id_number", "address_ka")


# ── Schemas ─────────────────────────────────────────────────────────────────


class CompanyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name_ka: str = Field(default="", max_length=255)
    name_en: str = Field(default="", max_length=255)
    director_ka: str = Field(default="", max_length=255)
    director_en: str = Field(default="", max_length=255)
    site: str = Field(default="", max_length=255)
    address_ka: str = Field(default="", max_length=500)
    address_en: str = Field(default="", max_length=500)
    id_number: str = Field(default="", max_length=50)
    email: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=50)
    whatsapp: str = Field(default="", max_length=50)


class BankAccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank_name: str = Field(default="", max_length=255)
    bank_iban: str = Field(default="", max_length=34)
    bank_swift: str = Field(default="", max_length=20)
    account_name: str = Field(default="", max_length=255)
    #: Exactly one account per company is primary — the one invoices print.
    is_primary: bool = False


class BankAccountOut(BaseModel):
    id: int
    bank_name: str
    #: Masked in the list. The full value is printed on an invoice, which is
    #: the only place it is needed in full.
    bank_iban_masked: str | None
    bank_swift: str
    account_name: str
    is_primary: bool


class CompanyOut(CompanyIn):
    id: int
    role: CompanyRole
    bank_accounts: list[BankAccountOut] = Field(default_factory=list)
    #: Which INVOICE_REQUIRED fields are still blank.
    missing_for_invoice: list[str] = Field(default_factory=list)


class InvoiceReadiness(BaseModel):
    ok: bool
    #: Human-readable, one line per problem. Rendered straight into the UI.
    problems: list[str]


class InvoiceParty(BaseModel):
    name: str
    address: str
    id_number: str
    email: str
    phone: str
    site: str


class InvoiceBank(BaseModel):
    bank_name: str
    #: In FULL. An invoice the client cannot pay from is not an invoice.
    iban: str
    swift: str
    account_name: str


class InvoiceLine(BaseModel):
    description: str
    languages: str
    pages: int
    unit_price: Decimal
    amount: Decimal


class InvoiceOut(BaseModel):
    #: Provisional — see the module docstring. Derived from the order id, not
    #: allocated from a sequence, and NOT a legal invoice number.
    number: str
    is_provisional: bool
    issued_on: date
    locale: Literal["ka", "en"]

    seller: InvoiceParty
    #: The trading name, when it differs from the legal entity.
    seller_brand: str | None
    buyer: InvoiceParty
    bank: InvoiceBank | None

    order_id: int
    order_date: date
    due_date: date | None

    lines: list[InvoiceLine]
    documents_total: Decimal
    delivery_cost: Decimal
    total: Decimal
    paid: Decimal
    balance_due: Decimal
    currency: str = "GEL"


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _company(db: Db, role: CompanyRole) -> Company | None:
    return (
        (await db.execute(select(Company).where(Company.role == role))).scalars().first()
    )


async def _accounts(db: Db, company_id: int) -> list[CompanyBankAccount]:
    rows = (
        (
            await db.execute(
                select(CompanyBankAccount)
                .where(CompanyBankAccount.company_id == company_id)
                .order_by(CompanyBankAccount.is_primary.desc(), CompanyBankAccount.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


def _missing(company: Company | None) -> list[str]:
    if company is None:
        return list(INVOICE_REQUIRED)
    return [f for f in INVOICE_REQUIRED if not str(getattr(company, f, "")).strip()]


async def _out(db: Db, company: Company) -> CompanyOut:
    accounts = await _accounts(db, company.id)
    return CompanyOut(
        id=company.id,
        role=company.role,  # type: ignore[arg-type]
        name_ka=company.name_ka,
        name_en=company.name_en,
        director_ka=company.director_ka,
        director_en=company.director_en,
        site=company.site,
        address_ka=company.address_ka,
        address_en=company.address_en,
        id_number=company.id_number,
        email=company.email,
        phone=company.phone,
        whatsapp=company.whatsapp,
        bank_accounts=[
            BankAccountOut(
                id=a.id,
                bank_name=a.bank_name,
                bank_iban_masked=mask_tail(a.bank_iban),
                bank_swift=a.bank_swift,
                account_name=a.account_name,
                is_primary=a.is_primary,
            )
            for a in accounts
        ],
        missing_for_invoice=_missing(company),
    )


# ── Company records ─────────────────────────────────────────────────────────


@router.get("", response_model=list[CompanyOut])
async def list_companies(
    db: Db,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> list[CompanyOut]:
    """Both roles, created or not.

    A role with no row yet comes back as a blank record rather than being
    omitted: the screen is a form for each, and an absent key would make the
    tab look broken on a fresh tenant.
    """
    out: list[CompanyOut] = []
    roles: tuple[CompanyRole, ...] = ("legal_entity", "brand")

    for role in roles:
        company = await _company(db, role)
        if company is None:
            # A blank record, so the form renders with the role already
            # chosen. id 0 means "not saved yet"; PUT creates it.
            out.append(
                CompanyOut(
                    id=0,
                    role=role,
                    missing_for_invoice=list(INVOICE_REQUIRED),
                )
            )
        else:
            out.append(await _out(db, company))
    return out


@router.put("/{role}", response_model=CompanyOut)
async def save_company(
    role: CompanyRole,
    payload: CompanyIn,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> CompanyOut:
    company = await _company(db, role)
    created = company is None

    if company is None:
        company = Company(role=role)
        db.add(company)
        await db.flush()

    before = {k: getattr(company, k) for k in payload.model_dump()}
    for field, value in payload.model_dump().items():
        setattr(company, field, value)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="company.created" if created else "company.updated",
        entity_type="company",
        entity_id=company.id,
        before=None if created else before,
        after=payload.model_dump(mode="json"),
    )
    return await _out(db, company)


# ── Bank accounts ───────────────────────────────────────────────────────────


@router.post(
    "/{role}/bank-accounts",
    response_model=BankAccountOut,
    status_code=http_status.HTTP_201_CREATED,
)
async def add_bank_account(
    role: CompanyRole,
    payload: BankAccountIn,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> BankAccountOut:
    company = await _company(db, role)
    if company is None:
        raise ConflictError("Save the company details before adding a bank account.")

    row = CompanyBankAccount(company_id=company.id, **payload.model_dump())
    db.add(row)
    await db.flush()

    # Exactly one primary. Demoting the others here rather than trusting the
    # caller: two primaries means the invoice picks one arbitrarily, and the
    # client pays into whichever the query happened to return first.
    if row.is_primary:
        for other in await _accounts(db, company.id):
            if other.id != row.id:
                other.is_primary = False
        await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="company_bank_account.created",
        entity_type="company_bank_account",
        entity_id=row.id,
        after={"bank_name": row.bank_name, "is_primary": row.is_primary},
    )

    return BankAccountOut(
        id=row.id,
        bank_name=row.bank_name,
        bank_iban_masked=mask_tail(row.bank_iban),
        bank_swift=row.bank_swift,
        account_name=row.account_name,
        is_primary=row.is_primary,
    )


@router.post("/bank-accounts/{account_id}/primary", response_model=BankAccountOut)
async def make_primary(
    account_id: int,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> BankAccountOut:
    row = await db.get(CompanyBankAccount, account_id)
    if row is None:
        raise NotFoundError("Bank account not found.")

    for other in await _accounts(db, row.company_id):
        other.is_primary = other.id == row.id
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="company_bank_account.made_primary",
        entity_type="company_bank_account",
        entity_id=row.id,
        after={"bank_name": row.bank_name},
    )

    return BankAccountOut(
        id=row.id,
        bank_name=row.bank_name,
        bank_iban_masked=mask_tail(row.bank_iban),
        bank_swift=row.bank_swift,
        account_name=row.account_name,
        is_primary=True,
    )


@router.delete("/bank-accounts/{account_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_bank_account(
    account_id: int,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> None:
    row = await db.get(CompanyBankAccount, account_id)
    if row is None:
        raise NotFoundError("Bank account not found.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="company_bank_account.deleted",
        entity_type="company_bank_account",
        entity_id=row.id,
        before={"bank_name": row.bank_name, "iban": mask_tail(row.bank_iban)},
    )
    await db.delete(row)


# ── Readiness ───────────────────────────────────────────────────────────────


@router.get("/invoice-readiness", response_model=InvoiceReadiness)
async def invoice_readiness(
    db: Db,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> InvoiceReadiness:
    """Can this bureau issue an invoice, and if not, what is missing?

    Asked by the Settings tab so the gap is visible before someone needs an
    invoice, rather than at the moment they are trying to send one.
    """
    labels = {
        "name_ka": "the legal entity's name",
        "id_number": "the tax / registration number",
        "address_ka": "the legal address",
    }

    legal = await _company(db, "legal_entity")
    problems = [f"Set {labels[f]}." for f in _missing(legal)]

    if legal is not None:
        accounts = await _accounts(db, legal.id)
        if not accounts:
            problems.append("Add a bank account — an invoice needs one to be payable.")
        elif not any(a.is_primary for a in accounts):
            problems.append("Mark one bank account as primary.")
        elif not next(a.bank_iban for a in accounts if a.is_primary).strip():
            problems.append("The primary bank account has no IBAN.")

    return InvoiceReadiness(ok=not problems, problems=problems)


# ── The invoice ─────────────────────────────────────────────────────────────

invoice_router = APIRouter(prefix="/orders", tags=["companies"])


def _party(company: Company | None, locale: str) -> InvoiceParty:
    """A company as it appears on the invoice, in one locale.

    Falls back across languages rather than printing a blank: a bureau that
    filled in only the Georgian name should get the Georgian name on an
    English invoice, not an empty line where the seller should be.
    """
    if company is None:
        return InvoiceParty(name="", address="", id_number="", email="", phone="", site="")

    preferred = company.name_en if locale == "en" else company.name_ka
    name = preferred or company.name_ka or company.name_en
    address = (
        (company.address_en if locale == "en" else company.address_ka)
        or company.address_ka
        or company.address_en
    )
    return InvoiceParty(
        name=name,
        address=address,
        id_number=company.id_number,
        email=company.email,
        phone=company.phone,
        site=company.site,
    )


@invoice_router.get("/{order_id}/invoice", response_model=InvoiceOut)
async def order_invoice(
    order_id: int,
    db: Db,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
    locale: Literal["ka", "en"] = "ka",
) -> InvoiceOut:
    """Assemble an invoice for one order.

    Amounts come from the order's STORED document costs — never recomputed —
    so a rate change since the order was placed cannot alter what the client
    is billed. That is the same guarantee the order screen makes, and it is
    the reason the pricing engine writes costs rather than deriving them.

    Refuses rather than issuing an incomplete document: an invoice missing the
    seller's tax number or bank account looks official and cannot be paid or
    filed, which is worse than no invoice at all.
    """
    stmt, _status_sq, _docs, _paid, _expenses = base_order_query()
    from sqlalchemy.orm import joinedload

    stmt = stmt.join(Client, Client.id == Order.client_id).options(joinedload(Order.client))
    row = (await db.execute(stmt.where(Order.id == order_id))).unique().first()
    if row is None:
        raise NotFoundError("Order not found.")

    order: Order = row[0]
    documents_total = Decimal(row[2] or 0)
    paid = Decimal(row[8] or 0)

    legal = await _company(db, "legal_entity")
    brand = await _company(db, "brand")

    missing = _missing(legal)
    if missing:
        raise ConflictError(
            "The invoicing company is incomplete: missing "
            f"{', '.join(missing)}. Fill it in under Settings → Companies."
        )

    assert legal is not None  # _missing() returns every field when it is None
    accounts = await _accounts(db, legal.id)
    primary = next((a for a in accounts if a.is_primary), None) or (
        accounts[0] if accounts else None
    )
    if primary is None or not primary.bank_iban.strip():
        raise ConflictError(
            "The invoicing company has no bank account with an IBAN. "
            "Add one under Settings → Companies."
        )

    docs = (
        (
            await db.execute(
                select(OrderDocument)
                .where(OrderDocument.order_id == order_id)
                .order_by(OrderDocument.id)
            )
        )
        .scalars()
        .all()
    )

    lines = [
        InvoiceLine(
            description=d.document_type.name_en
            if locale == "en" and d.document_type
            else (d.document_type.name_ka if d.document_type else "Translation"),
            languages=f"{d.source_language.upper()} → {d.target_language.upper()}",
            pages=d.page_count,
            # Per page, derived for display only. The authority is `amount`.
            unit_price=(d.price / d.page_count) if d.page_count else d.price,
            amount=d.price,
        )
        for d in docs
    ]

    total = documents_total + order.delivery_cost

    return InvoiceOut(
        # Provisional: derived from the order id, not allocated from a
        # sequence. See the module docstring before treating it as legal.
        number=f"{order.id}",
        is_provisional=True,
        issued_on=datetime.now(UTC).date(),
        locale=locale,
        seller=_party(legal, locale),
        seller_brand=(
            _party(brand, locale).name
            if brand and _party(brand, locale).name != _party(legal, locale).name
            else None
        ),
        buyer=InvoiceParty(
            name=order.client.name if order.client else "",
            address=(order.client.address or "") if order.client else "",
            id_number=(order.client.personal_number or "") if order.client else "",
            email=(order.client.email or "") if order.client else "",
            phone=(order.client.phone or "") if order.client else "",
            site="",
        ),
        bank=InvoiceBank(
            bank_name=primary.bank_name,
            # In full — the client has to pay into it.
            iban=primary.bank_iban,
            swift=primary.bank_swift,
            account_name=primary.account_name,
        ),
        order_id=order.id,
        order_date=order.order_date,
        due_date=order.due_date,
        lines=lines,
        documents_total=documents_total,
        delivery_cost=order.delivery_cost,
        total=total,
        paid=paid,
        balance_due=total - paid,
    )
