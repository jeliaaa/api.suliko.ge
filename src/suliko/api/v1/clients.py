"""Clients.

This is the reference implementation for every other resource router. The
patterns here — tenant-implicit queries, 404 for anything outside the tenant,
masked sensitive fields, cursorless keyset-free offset pagination matching the
UI's "Showing N of M" — are meant to be copied.

Note what is NOT in any query below: ``WHERE tenant_id = ...``. The ORM filter
in ``suliko.db.tenancy`` adds it. Writing it by hand here would be harmless but
misleading, because it would suggest that forgetting it is possible.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, or_, select

from suliko.api.deps import CurrentSession, Db, require
from suliko.core.errors import NotFoundError
from suliko.models.directory import Client, ClientType
from suliko.security.permissions import Permission

router = APIRouter(prefix="/clients", tags=["clients"])


def mask_personal_number(value: str | None) -> str | None:
    """Show only the last four digits in list views.

    A national ID is the kind of field that ends up in a screenshot, a
    support ticket or an exported spreadsheet. The full value is available
    through the detail endpoint, which is permissioned and audit-logged.
    """
    if not value:
        return None
    if len(value) <= 4:
        return "•" * len(value)
    return f"{'•' * (len(value) - 4)}{value[-4:]}"


class ClientBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    client_type: ClientType = ClientType.B2C
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    acquisition_source: str | None = Field(default=None, max_length=255)
    notes: str | None = None


class ClientCreate(ClientBase):
    personal_number: str | None = Field(default=None, max_length=50)

    # Reject unknown fields rather than ignoring them: a client sending
    # `tenant_id` or `id` gets a 422, not a silent no-op that looks like it
    # worked. This is the mass-assignment defence.
    model_config = ConfigDict(extra="forbid")


class ClientUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    client_type: ClientType | None = None
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=500)
    personal_number: str | None = Field(default=None, max_length=50)
    acquisition_source: str | None = Field(default=None, max_length=255)
    notes: str | None = None


class ClientSummary(BaseModel):
    """List representation. Sensitive fields are masked."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    client_type: ClientType
    email: str | None
    phone: str | None
    personal_number_masked: str | None


class ClientDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    client_type: ClientType
    email: str | None
    phone: str | None
    address: str | None
    personal_number_masked: str | None
    acquisition_source: str | None
    notes: str | None


class PageMeta(BaseModel):
    """Drives the UI's "Showing 20 of 688 clients"."""

    total: int
    limit: int
    offset: int


class ClientPage(BaseModel):
    items: list[ClientSummary]
    meta: PageMeta


def _to_summary(client: Client) -> ClientSummary:
    return ClientSummary(
        id=client.id,
        name=client.name,
        client_type=client.client_type,
        email=client.email,
        phone=client.phone,
        personal_number_masked=mask_personal_number(client.personal_number),
    )


def _to_detail(client: Client) -> ClientDetail:
    return ClientDetail(
        id=client.id,
        name=client.name,
        client_type=client.client_type,
        email=client.email,
        phone=client.phone,
        address=client.address,
        personal_number_masked=mask_personal_number(client.personal_number),
        acquisition_source=client.acquisition_source,
        notes=client.notes,
    )


@router.get("", response_model=ClientPage)
async def list_clients(
    db: Db,
    _: Annotated[object, Depends(require(Permission.CLIENTS_READ))],
    search: Annotated[str | None, Query(max_length=255)] = None,
    client_type: ClientType | None = None,
    sort: Literal["name", "-name", "id", "-id"] = "-id",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ClientPage:
    stmt = select(Client)

    if search:
        # ILIKE with a leading wildcard cannot use a btree index. Fine at the
        # current scale (~700 clients per tenant); when it stops being fine,
        # the fix is a pg_trgm GIN index, not a different query.
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Client.name.ilike(pattern),
                Client.email.ilike(pattern),
                Client.phone.ilike(pattern),
            )
        )

    if client_type is not None:
        stmt = stmt.where(Client.client_type == client_type)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    column = Client.name if sort.lstrip("-") == "name" else Client.id
    stmt = stmt.order_by(column.desc() if sort.startswith("-") else column.asc())

    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()

    return ClientPage(
        items=[_to_summary(c) for c in rows],
        meta=PageMeta(total=total, limit=limit, offset=offset),
    )


@router.get("/{client_id}", response_model=ClientDetail)
async def get_client(
    client_id: int,
    db: Db,
    _: Annotated[object, Depends(require(Permission.CLIENTS_READ))],
) -> ClientDetail:
    """Fetch one client.

    ``db.get`` is subject to the tenant filter, so another tenant's id returns
    None and therefore 404 — never 403, which would confirm the row exists.
    """
    client = await db.get(Client, client_id)
    if client is None:
        raise NotFoundError("Client not found.")
    return _to_detail(client)


@router.post("", response_model=ClientDetail, status_code=status.HTTP_201_CREATED)
async def create_client(
    payload: ClientCreate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.CLIENTS_WRITE))],
) -> ClientDetail:
    """Create a client.

    ``tenant_id`` is not accepted from the payload and is not set here: the
    before-flush hook in ``suliko.db.tenancy`` stamps it from the session.
    """
    client = Client(**payload.model_dump())
    db.add(client)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="client.created",
        entity_type="client",
        entity_id=client.id,
        after=payload.model_dump(mode="json"),
    )
    return _to_detail(client)


@router.patch("/{client_id}", response_model=ClientDetail)
async def update_client(
    client_id: int,
    payload: ClientUpdate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.CLIENTS_WRITE))],
) -> ClientDetail:
    client = await db.get(Client, client_id)
    if client is None:
        raise NotFoundError("Client not found.")

    changes = payload.model_dump(exclude_unset=True)
    before = {k: getattr(client, k) for k in changes}

    for field, value in changes.items():
        setattr(client, field, value)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="client.updated",
        entity_type="client",
        entity_id=client.id,
        before=before,
        after=changes,
    )
    return _to_detail(client)


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client(
    client_id: int,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.CLIENTS_WRITE))],
) -> None:
    client = await db.get(Client, client_id)
    if client is None:
        raise NotFoundError("Client not found.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="client.deleted",
        entity_type="client",
        entity_id=client.id,
        before={"name": client.name, "client_type": client.client_type.value},
    )
    # RESTRICT on orders.client_id means this raises rather than orphaning
    # orders. That surfaces as a 409 from the SQLAlchemy handler, which is the
    # correct answer: a client with history should be deactivated, not deleted.
    await db.delete(client)
