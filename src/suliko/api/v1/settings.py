"""Settings — the tabs from the production Settings screen.

General, document types, languages and language-pair pricing. Companies and
integrations are not here yet: company details only matter once invoices are
generated, and integration credentials need the envelope-encryption flow in
`core.crypto` plus a UI that never displays a stored secret.

Everything in this router is per-tenant. Two bureaus can price the same
language pair differently, and that is the point of the B2B pivot.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select

from suliko.api.deps import CurrentSession, Db, require
from suliko.core.errors import ConflictError, NotFoundError, ValidationError
from suliko.domain.clock import is_valid_zone
from suliko.models.reference import DocumentType, Language, LanguagePairPrice, TenantSettings
from suliko.models.tenant import Tenant
from suliko.security.permissions import Permission

router = APIRouter(prefix="/settings", tags=["settings"])


# ── General ─────────────────────────────────────────────────────────────────


class GeneralSettings(BaseModel):
    #: The bureau's name as the app shell and emails show it. Lives on the
    #: tenant row; edited here because it is the bureau's own setting.
    organisation_name: str
    #: IANA zone. Decides "today" everywhere — see `domain.clock`.
    timezone: str
    default_language: str
    system_email: str | None
    urgency_multiplier_standard: Decimal
    urgency_multiplier_express: Decimal
    urgency_multiplier_urgent: Decimal
    delivery_fee: Decimal
    default_translator_share: Decimal
    #: Days from order date to the default due date, per urgency.
    due_days_standard: int
    due_days_express: int
    due_days_urgent: int


class GeneralSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_language: str | None = Field(default=None, min_length=2, max_length=5)
    system_email: EmailStr | None = None
    # Bounded deliberately. A multiplier of 0 would make work free and a
    # fat-fingered 100 would quote six figures; neither should be reachable by
    # a typo in a settings form.
    urgency_multiplier_standard: Decimal | None = Field(default=None, ge=Decimal("0.1"), le=10)
    urgency_multiplier_express: Decimal | None = Field(default=None, ge=Decimal("0.1"), le=10)
    urgency_multiplier_urgent: Decimal | None = Field(default=None, ge=Decimal("0.1"), le=10)
    delivery_fee: Decimal | None = Field(default=None, ge=0, le=10_000)
    #: What share of the translation fee the translator receives.
    default_translator_share: Decimal | None = Field(
        default=None, ge=0, le=1, max_digits=4, decimal_places=3
    )
    due_days_standard: int | None = Field(default=None, ge=0, le=365)
    due_days_express: int | None = Field(default=None, ge=0, le=365)
    due_days_urgent: int | None = Field(default=None, ge=0, le=365)
    organisation_name: str | None = Field(default=None, min_length=1, max_length=255)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)


#: Fields that live on the tenant row rather than the settings row.
_TENANT_FIELDS = ("organisation_name", "timezone")


def _general_out(row: TenantSettings, tenant: Tenant) -> GeneralSettings:
    return GeneralSettings(
        organisation_name=tenant.display_name,
        timezone=tenant.timezone,
        default_language=row.default_language,
        system_email=row.system_email,
        urgency_multiplier_standard=row.urgency_multiplier_standard,
        urgency_multiplier_express=row.urgency_multiplier_express,
        urgency_multiplier_urgent=row.urgency_multiplier_urgent,
        delivery_fee=row.delivery_fee,
        default_translator_share=row.default_translator_share,
        due_days_standard=row.due_days_standard,
        due_days_express=row.due_days_express,
        due_days_urgent=row.due_days_urgent,
    )


async def _tenant(db: Db, session: CurrentSession) -> Tenant:
    tenant = await db.get(Tenant, session.tenant_id)
    if tenant is None:  # pragma: no cover - a live session always has one
        raise NotFoundError("Organisation not found.")
    return tenant


async def _settings_row(db: Db) -> TenantSettings:
    """The tenant's settings row, created on first access.

    Lazily created rather than required at tenant setup, so a tenant made
    before this table existed still works.
    """
    row = (await db.execute(select(TenantSettings))).scalars().first()
    if row is None:
        row = TenantSettings()
        db.add(row)
        await db.flush()
    return row


@router.get("/general", response_model=GeneralSettings)
async def get_general(
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.SETTINGS_MANAGE))],
) -> GeneralSettings:
    row = await _settings_row(db)
    return _general_out(row, await _tenant(db, session))


@router.patch("/general", response_model=GeneralSettings)
async def update_general(
    payload: GeneralSettingsUpdate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> GeneralSettings:
    row = await _settings_row(db)
    tenant = await _tenant(db, session)
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None and key not in ("system_email",):
            raise ValidationError(f"{key} cannot be empty.")
    if "timezone" in changes and not is_valid_zone(str(changes["timezone"])):
        raise ValidationError(
            f"Unknown timezone {changes['timezone']!r}. Use an IANA name such as Asia/Tbilisi."
        )

    before = {
        k: (tenant.display_name if k == "organisation_name" else getattr(tenant, k))
        if k in _TENANT_FIELDS
        else getattr(row, k)
        for k in changes
    }

    for field, value in changes.items():
        if field == "organisation_name":
            tenant.display_name = str(value).strip()
        elif field == "timezone":
            tenant.timezone = str(value)
        else:
            setattr(row, field, value)
    await db.flush()

    from suliko.core.audit import record

    # Pricing knobs change what every future order costs, so this one is
    # audited with before/after rather than just noted.
    await record(
        db,
        session,
        action="settings.updated",
        entity_type="tenant_settings",
        entity_id=row.id,
        before=before,
        after=changes,
    )
    return _general_out(row, tenant)


# ── Document types ──────────────────────────────────────────────────────────


class DocumentTypeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name_en: str = Field(min_length=1, max_length=255)
    name_ka: str = Field(min_length=1, max_length=255)
    price_multiplier: Decimal = Field(default=Decimal("1.0"), ge=Decimal("0.1"), le=100)
    is_active: bool = True


class DocumentTypeOut(BaseModel):
    id: int
    name_en: str
    name_ka: str
    price_multiplier: Decimal
    is_active: bool


@router.get("/document-types", response_model=list[DocumentTypeOut])
async def list_document_types(
    db: Db,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> list[DocumentTypeOut]:
    rows = (await db.execute(select(DocumentType).order_by(DocumentType.name_en))).scalars().all()
    return [DocumentTypeOut.model_validate(r, from_attributes=True) for r in rows]


@router.post(
    "/document-types", response_model=DocumentTypeOut, status_code=http_status.HTTP_201_CREATED
)
async def create_document_type(
    payload: DocumentTypeIn,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> DocumentTypeOut:
    row = DocumentType(**payload.model_dump())
    db.add(row)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="document_type.created",
        entity_type="document_type",
        entity_id=row.id,
        after=payload.model_dump(mode="json"),
    )
    return DocumentTypeOut.model_validate(row, from_attributes=True)


@router.patch("/document-types/{type_id}", response_model=DocumentTypeOut)
async def update_document_type(
    type_id: int,
    payload: DocumentTypeIn,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> DocumentTypeOut:
    row = await db.get(DocumentType, type_id)
    if row is None:
        raise NotFoundError("Document type not found.")

    before = {k: getattr(row, k) for k in payload.model_dump()}
    for field, value in payload.model_dump().items():
        setattr(row, field, value)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="document_type.updated",
        entity_type="document_type",
        entity_id=row.id,
        before=before,
        after=payload.model_dump(mode="json"),
    )
    return DocumentTypeOut.model_validate(row, from_attributes=True)


# ── Languages ───────────────────────────────────────────────────────────────


class LanguageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=2, max_length=5, pattern=r"^[a-z]{2,5}$")
    name_en: str = Field(min_length=1, max_length=100)
    name_ka: str = Field(min_length=1, max_length=100)
    is_active: bool = True


class LanguageOut(BaseModel):
    id: int
    code: str
    name_en: str
    name_ka: str
    is_active: bool


@router.get("/languages", response_model=list[LanguageOut])
async def list_languages(
    db: Db,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> list[LanguageOut]:
    rows = (await db.execute(select(Language).order_by(Language.name_en))).scalars().all()
    return [LanguageOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/languages", response_model=LanguageOut, status_code=http_status.HTTP_201_CREATED)
async def create_language(
    payload: LanguageIn,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> LanguageOut:
    existing = (
        (await db.execute(select(Language).where(Language.code == payload.code))).scalars().first()
    )
    if existing:
        raise ConflictError(f"Language {payload.code!r} already exists.")

    row = Language(**payload.model_dump())
    db.add(row)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="language.created",
        entity_type="language",
        entity_id=row.id,
        after=payload.model_dump(mode="json"),
    )
    return LanguageOut.model_validate(row, from_attributes=True)


@router.patch("/languages/{language_id}", response_model=LanguageOut)
async def update_language(
    language_id: int,
    payload: LanguageIn,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> LanguageOut:
    """Rename a language, or take it out of use.

    There is deliberately no DELETE. `language_pair_prices`, `order_documents`
    and every historical order store the *code*, so removing the row would
    leave orders referring to a language nobody can name any more. Clearing
    `is_active` takes it out of the dropdowns while history keeps reading
    correctly, which is what "remove a language" actually means here.
    """
    row = await db.get(Language, language_id)
    if row is None:
        raise NotFoundError("Language not found.")

    code = payload.code.lower()

    # The code is what pricing and documents join on, so a change to it is a
    # rename of the thing itself, not a relabelling — and a collision would
    # silently merge two languages.
    if code != row.code:
        clash = (await db.execute(select(Language).where(Language.code == code))).scalars().first()
        if clash:
            raise ConflictError(f"Language {code!r} already exists.")

    before = {k: getattr(row, k) for k in payload.model_dump()}

    row.code = code
    row.name_en = payload.name_en
    row.name_ka = payload.name_ka
    row.is_active = payload.is_active
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="language.updated",
        entity_type="language",
        entity_id=row.id,
        before=before,
        after=payload.model_dump(mode="json"),
    )
    return LanguageOut.model_validate(row, from_attributes=True)


# ── Language-pair pricing ───────────────────────────────────────────────────


class PairPriceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_language: str = Field(min_length=2, max_length=5)
    target_language: str = Field(min_length=2, max_length=5)
    price_per_page: Decimal = Field(ge=0, le=100_000)
    is_active: bool = True


class PairPriceOut(BaseModel):
    id: int
    source_language: str
    target_language: str
    price_per_page: Decimal
    is_active: bool


@router.get("/pricing", response_model=list[PairPriceOut])
async def list_pricing(
    db: Db,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> list[PairPriceOut]:
    rows = (
        (
            await db.execute(
                select(LanguagePairPrice).order_by(
                    LanguagePairPrice.source_language, LanguagePairPrice.target_language
                )
            )
        )
        .scalars()
        .all()
    )
    return [PairPriceOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/pricing", response_model=PairPriceOut, status_code=http_status.HTTP_201_CREATED)
async def create_pair_price(
    payload: PairPriceIn,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> PairPriceOut:
    source = payload.source_language.lower()
    target = payload.target_language.lower()

    if source == target:
        raise ConflictError("Source and target language cannot be the same.")

    existing = (
        (
            await db.execute(
                select(LanguagePairPrice).where(
                    LanguagePairPrice.source_language == source,
                    LanguagePairPrice.target_language == target,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing:
        raise ConflictError(f"A rate for {source}-{target} already exists.")

    row = LanguagePairPrice(
        source_language=source,
        target_language=target,
        price_per_page=payload.price_per_page,
        is_active=payload.is_active,
    )
    db.add(row)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="pricing.created",
        entity_type="language_pair_price",
        entity_id=row.id,
        after=payload.model_dump(mode="json"),
    )
    return PairPriceOut.model_validate(row, from_attributes=True)


@router.patch("/pricing/{pair_id}", response_model=PairPriceOut)
async def update_pair_price(
    pair_id: int,
    payload: PairPriceIn,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> PairPriceOut:
    row = await db.get(LanguagePairPrice, pair_id)
    if row is None:
        raise NotFoundError("Rate not found.")

    before = {
        "source_language": row.source_language,
        "target_language": row.target_language,
        "price_per_page": row.price_per_page,
        "is_active": row.is_active,
    }

    row.source_language = payload.source_language.lower()
    row.target_language = payload.target_language.lower()
    row.price_per_page = payload.price_per_page
    row.is_active = payload.is_active
    await db.flush()

    from suliko.core.audit import record

    # Rate changes affect every future quote, so before/after is recorded.
    # Existing orders are unaffected: their costs were stored at creation.
    await record(
        db,
        session,
        action="pricing.updated",
        entity_type="language_pair_price",
        entity_id=row.id,
        before=before,
        after=payload.model_dump(mode="json"),
    )
    return PairPriceOut.model_validate(row, from_attributes=True)


@router.delete("/pricing/{pair_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_pair_price(
    pair_id: int,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.SETTINGS_MANAGE))],
) -> None:
    row = await db.get(LanguagePairPrice, pair_id)
    if row is None:
        raise NotFoundError("Rate not found.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="pricing.deleted",
        entity_type="language_pair_price",
        entity_id=row.id,
        before={"pair": f"{row.source_language}-{row.target_language}"},
    )
    await db.delete(row)
