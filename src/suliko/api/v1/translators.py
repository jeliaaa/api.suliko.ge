"""Translators.

Follows `clients.py`. The differences that matter:

- Bank details are masked in the list (an IBAN is a payment credential, and
  list responses end up in exports and screenshots).
- `has_portal_account` is derived, not stored — it drives the Active/None badge
  the production screen shows, and asking "is a username set" is clearer than
  making callers infer it.
- The portal password is never accepted or returned here. Setting a
  translator's portal credentials is a separate, audited action.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, or_, select

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._shared import PageMeta, mask_tail
from suliko.core.errors import NotFoundError
from suliko.models.directory import Translator
from suliko.security.permissions import Permission

router = APIRouter(prefix="/translators", tags=["translators"])


class TranslatorBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: EmailStr | None = None
    office_address: str | None = Field(default=None, max_length=255)
    comment: str | None = None
    experience_from: date | None = None
    is_active: bool = True
    default_rate: Decimal | None = Field(default=None, ge=0, le=100000)

    bank_iban: str | None = Field(default=None, max_length=34)
    bank_inn: str | None = Field(default=None, max_length=20)
    bank_code: str | None = Field(default=None, max_length=20)


class TranslatorCreate(TranslatorBase):
    pass


class TranslatorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: EmailStr | None = None
    office_address: str | None = Field(default=None, max_length=255)
    comment: str | None = None
    experience_from: date | None = None
    is_active: bool | None = None
    default_rate: Decimal | None = Field(default=None, ge=0, le=100000)
    bank_iban: str | None = Field(default=None, max_length=34)
    bank_inn: str | None = Field(default=None, max_length=20)
    bank_code: str | None = Field(default=None, max_length=20)


class TranslatorSummary(BaseModel):
    id: int
    name: str
    phone: str | None
    email: str | None
    is_active: bool
    #: Drives the Active / None account badge.
    has_portal_account: bool
    bank_iban_masked: str | None


class TranslatorDetail(TranslatorSummary):
    office_address: str | None
    comment: str | None
    experience_from: date | None
    default_rate: Decimal | None
    bank_inn: str | None
    bank_code: str | None
    portal_username: str | None


class TranslatorPage(BaseModel):
    items: list[TranslatorSummary]
    meta: PageMeta


def _summary(row: Translator) -> TranslatorSummary:
    return TranslatorSummary(
        id=row.id,
        name=row.name,
        phone=row.phone,
        email=row.email,
        is_active=row.is_active,
        has_portal_account=row.has_portal_account,
        bank_iban_masked=mask_tail(row.bank_iban),
    )


def _detail(row: Translator) -> TranslatorDetail:
    return TranslatorDetail(
        **_summary(row).model_dump(),
        office_address=row.office_address,
        comment=row.comment,
        experience_from=row.experience_from,
        default_rate=row.default_rate,
        bank_inn=row.bank_inn,
        bank_code=row.bank_code,
        portal_username=row.portal_username,
    )


@router.get("", response_model=TranslatorPage)
async def list_translators(
    db: Db,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_READ))],
    search: Annotated[str | None, Query(max_length=255)] = None,
    is_active: bool | None = None,
    sort: Literal["name", "-name", "id", "-id"] = "-id",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TranslatorPage:
    stmt = select(Translator)

    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                Translator.name.ilike(pattern),
                Translator.email.ilike(pattern),
                Translator.phone.ilike(pattern),
            )
        )

    if is_active is not None:
        stmt = stmt.where(Translator.is_active == is_active)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    column = Translator.name if sort.lstrip("-") == "name" else Translator.id
    stmt = stmt.order_by(column.desc() if sort.startswith("-") else column.asc())

    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()

    return TranslatorPage(
        items=[_summary(r) for r in rows],
        meta=PageMeta(total=total, limit=limit, offset=offset),
    )


@router.get("/{translator_id}", response_model=TranslatorDetail)
async def get_translator(
    translator_id: int,
    db: Db,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_READ))],
) -> TranslatorDetail:
    row = await db.get(Translator, translator_id)
    if row is None:
        raise NotFoundError("Translator not found.")
    return _detail(row)


@router.post("", response_model=TranslatorDetail, status_code=status.HTTP_201_CREATED)
async def create_translator(
    payload: TranslatorCreate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_WRITE))],
) -> TranslatorDetail:
    row = Translator(**payload.model_dump())
    db.add(row)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="translator.created",
        entity_type="translator",
        entity_id=row.id,
        after=payload.model_dump(mode="json"),
    )
    return _detail(row)


@router.patch("/{translator_id}", response_model=TranslatorDetail)
async def update_translator(
    translator_id: int,
    payload: TranslatorUpdate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_WRITE))],
) -> TranslatorDetail:
    row = await db.get(Translator, translator_id)
    if row is None:
        raise NotFoundError("Translator not found.")

    changes = payload.model_dump(exclude_unset=True)
    before = {k: getattr(row, k) for k in changes}

    for field, value in changes.items():
        setattr(row, field, value)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="translator.updated",
        entity_type="translator",
        entity_id=row.id,
        before=before,
        after=changes,
    )
    return _detail(row)


@router.delete("/{translator_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_translator(
    translator_id: int,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.TRANSLATORS_WRITE))],
) -> None:
    row = await db.get(Translator, translator_id)
    if row is None:
        raise NotFoundError("Translator not found.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="translator.deleted",
        entity_type="translator",
        entity_id=row.id,
        before={"name": row.name},
    )
    # order_documents.translator_id is ON DELETE SET NULL, so past work is
    # kept and simply becomes unassigned. Deactivating (is_active=false) is
    # still the better move for someone who has history.
    await db.delete(row)
