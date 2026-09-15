"""Quote calculator.

"Quick quote — no order created", as the production screen says. Nothing here
writes anything; it prices a hypothetical order and returns the working.

## Why this is a server endpoint and not TypeScript

The PHP does this arithmetic in browser JavaScript, which is why the formula
ended up living in four places and drifting. Putting it here means the
calculator, the order builder and the invoice all price through the SAME
tested implementation in `suliko.domain.pricing`.

A round trip per keystroke would be laggy, so the frontend debounces. That is
a far smaller cost than a second copy of a money formula that nobody notices
has diverged until a client disputes an invoice.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from suliko.api.deps import CurrentSession, Db, require
from suliko.domain.pricing import (
    DocumentPricingInput,
    PricingConfig,
    quote_order,
)
from suliko.models.order import CopyType, HandoverMethod, Urgency
from suliko.models.reference import DocumentType, LanguagePairPrice, TenantSettings
from suliko.security.permissions import Permission

router = APIRouter(prefix="/calculator", tags=["calculator"])

MAX_DOCUMENTS = 50


class QuoteDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_language: str = Field(min_length=2, max_length=5)
    target_language: str = Field(min_length=2, max_length=5)
    document_type_id: int
    page_count: int = Field(ge=1, le=10_000)
    copy_type: CopyType = CopyType.ORIGINAL


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[QuoteDocument] = Field(min_length=1, max_length=MAX_DOCUMENTS)
    urgency: Urgency = Urgency.STANDARD
    handover_method: HandoverMethod = HandoverMethod.SCAN


class QuoteLine(BaseModel):
    translation_cost: Decimal
    notary_cost: Decimal
    certification_cost: Decimal
    price: Decimal
    translator_cost: Decimal
    base_rate_per_page: Decimal
    document_type_multiplier: Decimal
    urgency_multiplier: Decimal
    page_count: int
    is_notarized: bool
    #: True when the pair has no configured rate and a fallback was used, so
    #: the UI can warn instead of quoting a number nobody agreed to.
    used_fallback_rate: bool


class QuoteResponse(BaseModel):
    documents: list[QuoteLine]
    documents_total: Decimal
    delivery_cost: Decimal
    total: Decimal
    translator_cost_total: Decimal
    notary_cost_total: Decimal
    gross_profit: Decimal
    currency: str = "GEL"


async def _pricing_config(db: Db) -> PricingConfig:
    """Per-tenant multipliers, falling back to the documented defaults."""
    settings = (await db.execute(select(TenantSettings))).scalars().first()
    if settings is None:
        return PricingConfig.defaults()

    return PricingConfig(
        urgency_multipliers={
            Urgency.STANDARD: Decimal(str(settings.urgency_multiplier_standard)),
            Urgency.EXPRESS: Decimal(str(settings.urgency_multiplier_express)),
            Urgency.URGENT: Decimal(str(settings.urgency_multiplier_urgent)),
        },
        delivery_fee=Decimal(str(settings.delivery_fee)),
        translator_share=Decimal(str(settings.default_translator_share)),
    )


@router.post("/quote", response_model=QuoteResponse)
async def quote(
    payload: QuoteRequest,
    db: Db,
    _session: CurrentSession,
    __: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> QuoteResponse:
    config = await _pricing_config(db)

    # Load every rate and multiplier the request touches in two queries rather
    # than one per line — a 50-document quote should not be 100 round trips.
    pair_keys = {(d.source_language.lower(), d.target_language.lower()) for d in payload.documents}
    rates = {
        (row.source_language.lower(), row.target_language.lower()): Decimal(str(row.price_per_page))
        for row in (
            await db.execute(select(LanguagePairPrice).where(LanguagePairPrice.is_active))
        ).scalars()
        if (row.source_language.lower(), row.target_language.lower()) in pair_keys
    }

    type_ids = {d.document_type_id for d in payload.documents}
    multipliers = {
        row.id: Decimal(str(row.price_multiplier))
        for row in (
            await db.execute(select(DocumentType).where(DocumentType.id.in_(type_ids)))
        ).scalars()
    }

    inputs = [
        DocumentPricingInput(
            page_count=d.page_count,
            # None when the pair is not configured — the engine then applies
            # its documented fallback and flags it rather than silently
            # inventing a rate.
            base_rate_per_page=rates.get((d.source_language.lower(), d.target_language.lower())),
            document_type_multiplier=multipliers.get(d.document_type_id, Decimal("1.0")),
            copy_type=d.copy_type,
            urgency=payload.urgency,
        )
        for d in payload.documents
    ]

    result = quote_order(inputs, payload.handover_method, config)

    return QuoteResponse(
        documents=[
            QuoteLine(
                translation_cost=b.translation_cost,
                notary_cost=b.notary_cost,
                certification_cost=b.certification_cost,
                price=b.price,
                translator_cost=b.translator_cost,
                base_rate_per_page=b.base_rate_per_page,
                document_type_multiplier=b.document_type_multiplier,
                urgency_multiplier=b.urgency_multiplier,
                page_count=b.page_count,
                is_notarized=b.is_notarized,
                used_fallback_rate=b.used_fallback_rate,
            )
            for b in result.documents
        ],
        documents_total=result.documents_total,
        delivery_cost=result.delivery_cost,
        total=result.total,
        translator_cost_total=result.translator_cost_total,
        notary_cost_total=result.notary_cost_total,
        gross_profit=result.gross_profit,
    )
