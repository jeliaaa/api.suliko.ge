"""Read-only lookup data.

Everything the order builder and the calculator need to render their dropdowns:
document types with their multipliers, active language pairs with their rates,
languages, and the fixed enums.

Mirrors the PHP's `/api/reference.php`, which exists for the same reason —
callers should not hardcode document-type ids or rates, because both live in
the database and change.

One request, cached briefly. This data changes when someone edits Settings,
not per page view.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select

from suliko.api.deps import Db, require
from suliko.models.order import CopyType, HandoverMethod, Urgency
from suliko.models.reference import DocumentType, Language, LanguagePairPrice, TenantSettings
from suliko.security.permissions import Permission

router = APIRouter(prefix="/reference", tags=["reference"])


class LanguageOut(BaseModel):
    code: str
    name_en: str
    name_ka: str


class DocumentTypeOut(BaseModel):
    id: int
    name_en: str
    name_ka: str
    price_multiplier: Decimal


class LanguagePairOut(BaseModel):
    source_language: str
    target_language: str
    price_per_page: Decimal


class EnumOption(BaseModel):
    value: str
    label: str
    #: Urgency carries its multiplier, handover its surcharge, copy type
    #: whether it implies notarisation. One shape, so the UI can render any of
    #: them with the same component.
    multiplier: Decimal | None = None
    extra_cost: Decimal | None = None
    notarized: bool | None = None
    requires_address: bool | None = None


class ReferenceData(BaseModel):
    currency: str = "GEL"
    languages: list[LanguageOut]
    document_types: list[DocumentTypeOut]
    language_pairs: list[LanguagePairOut]
    urgency_levels: list[EnumOption]
    handover_methods: list[EnumOption]
    copy_types: list[EnumOption]


@router.get("", response_model=ReferenceData)
async def get_reference(
    db: Db,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> ReferenceData:
    languages = [
        LanguageOut(code=row.code, name_en=row.name_en, name_ka=row.name_ka)
        for row in (
            await db.execute(select(Language).where(Language.is_active).order_by(Language.name_en))
        ).scalars()
    ]

    document_types = [
        DocumentTypeOut(
            id=row.id,
            name_en=row.name_en,
            name_ka=row.name_ka,
            price_multiplier=Decimal(str(row.price_multiplier)),
        )
        for row in (
            await db.execute(
                select(DocumentType).where(DocumentType.is_active).order_by(DocumentType.name_en)
            )
        ).scalars()
    ]

    language_pairs = [
        LanguagePairOut(
            source_language=row.source_language,
            target_language=row.target_language,
            price_per_page=Decimal(str(row.price_per_page)),
        )
        for row in (
            await db.execute(
                select(LanguagePairPrice)
                .where(LanguagePairPrice.is_active)
                .order_by(LanguagePairPrice.source_language, LanguagePairPrice.target_language)
            )
        ).scalars()
    ]

    settings = (await db.execute(select(TenantSettings))).scalars().first()
    standard = Decimal(str(settings.urgency_multiplier_standard)) if settings else Decimal("1.0")
    express = Decimal(str(settings.urgency_multiplier_express)) if settings else Decimal("1.5")
    urgent = Decimal(str(settings.urgency_multiplier_urgent)) if settings else Decimal("2.0")
    delivery_fee = Decimal(str(settings.delivery_fee)) if settings else Decimal("10")

    return ReferenceData(
        languages=languages,
        document_types=document_types,
        language_pairs=language_pairs,
        urgency_levels=[
            EnumOption(
                value=Urgency.STANDARD.value,
                label="Standard (3-5 business days)",
                multiplier=standard,
            ),
            EnumOption(
                value=Urgency.EXPRESS.value,
                label="Express (1-2 business days)",
                multiplier=express,
            ),
            EnumOption(value=Urgency.URGENT.value, label="Urgent (same day)", multiplier=urgent),
        ],
        handover_methods=[
            EnumOption(
                value=HandoverMethod.SCAN.value,
                label="Scan (sent by email)",
                extra_cost=Decimal("0"),
                requires_address=False,
            ),
            EnumOption(
                value=HandoverMethod.PICKUP.value,
                label="Pick up from the office",
                extra_cost=Decimal("0"),
                requires_address=False,
            ),
            EnumOption(
                value=HandoverMethod.DELIVERY.value,
                label="Courier delivery",
                extra_cost=delivery_fee,
                requires_address=True,
            ),
        ],
        copy_types=[
            EnumOption(value=CopyType.ORIGINAL.value, label="Original document", notarized=False),
            EnumOption(value=CopyType.PLAIN.value, label="Photocopy", notarized=False),
            EnumOption(
                value=CopyType.NOTARY_ORIGINAL.value, label="Notary on original", notarized=True
            ),
            EnumOption(value=CopyType.NOTARY_COPY.value, label="Notary on copy", notarized=True),
            EnumOption(
                value=CopyType.NOTARY_CERTIFIED.value,
                label="Certified copy (notarised)",
                notarized=True,
            ),
        ],
    )
