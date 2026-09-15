"""Notaries.

Admin-managed identity and bank details only. Notaries deliberately have no
portal login — they do no AI translation and nobody has asked for notary
self-service. Do not add one without a reason.

`bank_ready` mirrors the Ready / No IBAN badge the production screen shows: a
notary without an IBAN cannot be paid by transfer, which is the thing the
person looking at that list actually wants to know.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, or_, select

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._shared import PageMeta, mask_tail
from suliko.core.errors import NotFoundError
from suliko.models.directory import Notary
from suliko.security.permissions import Permission

router = APIRouter(prefix="/notaries", tags=["notaries"])


class NotaryBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: EmailStr | None = None
    registration_number: str | None = Field(default=None, max_length=100)
    office_address: str | None = Field(default=None, max_length=255)
    comment: str | None = None
    bank_iban: str | None = Field(default=None, max_length=34)
    bank_inn: str | None = Field(default=None, max_length=20)
    bank_code: str | None = Field(default=None, max_length=20)


class NotaryCreate(NotaryBase):
    pass


class NotaryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: EmailStr | None = None
    registration_number: str | None = Field(default=None, max_length=100)
    office_address: str | None = Field(default=None, max_length=255)
    comment: str | None = None
    bank_iban: str | None = Field(default=None, max_length=34)
    bank_inn: str | None = Field(default=None, max_length=20)
    bank_code: str | None = Field(default=None, max_length=20)


class NotarySummary(BaseModel):
    id: int
    name: str
    phone: str | None
    email: str | None
    registration_number: str | None
    #: True when an IBAN is on file — the Ready / No IBAN badge.
    bank_ready: bool
    bank_iban_masked: str | None


class NotaryDetail(NotarySummary):
    office_address: str | None
    comment: str | None
    bank_inn: str | None
    bank_code: str | None


class NotaryPage(BaseModel):
    items: list[NotarySummary]
    meta: PageMeta


def _summary(row: Notary) -> NotarySummary:
    return NotarySummary(
        id=row.id,
        name=row.name,
        phone=row.phone,
        email=row.email,
        registration_number=row.registration_number,
        bank_ready=row.bank_ready,
        bank_iban_masked=mask_tail(row.bank_iban),
    )


def _detail(row: Notary) -> NotaryDetail:
    return NotaryDetail(
        **_summary(row).model_dump(),
        office_address=row.office_address,
        comment=row.comment,
        bank_inn=row.bank_inn,
        bank_code=row.bank_code,
    )


@router.get("", response_model=NotaryPage)
async def list_notaries(
    db: Db,
    _: Annotated[object, Depends(require(Permission.NOTARIES_READ))],
    search: Annotated[str | None, Query(max_length=255)] = None,
    sort: Literal["name", "-name", "id", "-id"] = "name",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NotaryPage:
    stmt = select(Notary)

    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Notary.name.ilike(pattern),
                Notary.email.ilike(pattern),
                Notary.registration_number.ilike(pattern),
            )
        )

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    column = Notary.name if sort.lstrip("-") == "name" else Notary.id
    stmt = stmt.order_by(column.desc() if sort.startswith("-") else column.asc())

    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()

    return NotaryPage(
        items=[_summary(r) for r in rows],
        meta=PageMeta(total=total, limit=limit, offset=offset),
    )


@router.get("/{notary_id}", response_model=NotaryDetail)
async def get_notary(
    notary_id: int,
    db: Db,
    _: Annotated[object, Depends(require(Permission.NOTARIES_READ))],
) -> NotaryDetail:
    row = await db.get(Notary, notary_id)
    if row is None:
        raise NotFoundError("Notary not found.")
    return _detail(row)


@router.post("", response_model=NotaryDetail, status_code=status.HTTP_201_CREATED)
async def create_notary(
    payload: NotaryCreate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.NOTARIES_WRITE))],
) -> NotaryDetail:
    row = Notary(**payload.model_dump())
    db.add(row)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="notary.created",
        entity_type="notary",
        entity_id=row.id,
        after=payload.model_dump(mode="json"),
    )
    return _detail(row)


@router.patch("/{notary_id}", response_model=NotaryDetail)
async def update_notary(
    notary_id: int,
    payload: NotaryUpdate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.NOTARIES_WRITE))],
) -> NotaryDetail:
    row = await db.get(Notary, notary_id)
    if row is None:
        raise NotFoundError("Notary not found.")

    changes = payload.model_dump(exclude_unset=True)
    before = {k: getattr(row, k) for k in changes}

    for field, value in changes.items():
        setattr(row, field, value)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="notary.updated",
        entity_type="notary",
        entity_id=row.id,
        before=before,
        after=changes,
    )
    return _detail(row)


@router.delete("/{notary_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notary(
    notary_id: int,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.NOTARIES_WRITE))],
) -> None:
    row = await db.get(Notary, notary_id)
    if row is None:
        raise NotFoundError("Notary not found.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="notary.deleted",
        entity_type="notary",
        entity_id=row.id,
        before={"name": row.name},
    )
    # order_documents.notary_id is ON DELETE SET NULL: the fee stays on the
    # document and simply becomes unattributed, which the Finances screen
    # already reports as "Unattributed Notary Fees".
    await db.delete(row)
