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

from suliko.api.deps import CurrentSession, Db, require
from suliko.core.errors import ValidationError
from suliko.domain.pricing import DocumentPricingInput, quote_order
from suliko.domain.pricing_context import load_pricing_context
from suliko.models.order import CopyType, HandoverMethod, Urgency
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
    #: Overrides what the copy type implies — a notarised translation of an
    #: original. None = derive from the copy type.
    is_notarized: bool | None = None


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
    #: Null without `reports.profit`.
    translator_cost: Decimal | None
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
    #: Null without `reports.profit`. The notary cost stays: it is part of the
    #: price the client is quoted, not the bureau's margin.
    translator_cost_total: Decimal | None
    notary_cost_total: Decimal
    gross_profit: Decimal | None
    currency: str = "GEL"


@router.post("/quote", response_model=QuoteResponse)
async def quote(
    payload: QuoteRequest,
    db: Db,
    session: CurrentSession,
    __: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> QuoteResponse:
    type_ids = {d.document_type_id for d in payload.documents}
    # The same loader order creation uses, so the quote and the stored order
    # cannot read the tenant's settings two different ways.
    ctx = await load_pricing_context(db, session.plan, type_ids)
    missing = type_ids - set(ctx.multipliers)
    if missing:
        # Creating the order would refuse these; quoting them at a made-up
        # multiplier of 1.0 would show a price the order then cannot have.
        raise ValidationError(f"Unknown document type(s): {sorted(missing)}")

    inputs = [
        DocumentPricingInput(
            page_count=d.page_count,
            # None when the pair is not configured — the engine then applies
            # its documented fallback and flags it rather than silently
            # inventing a rate. (Creating the order refuses it without a
            # typed-in price.)
            base_rate_per_page=ctx.rate(d.source_language, d.target_language),
            document_type_multiplier=ctx.multipliers[d.document_type_id],
            copy_type=d.copy_type,
            urgency=payload.urgency,
            is_notarized=d.is_notarized,
        )
        for d in payload.documents
    ]

    result = quote_order(inputs, payload.handover_method, ctx.config)
    # What the job would COST is margin, and staff without reports.profit do
    # not see margins — the same rule the order endpoints apply.
    show_costs = session.has(Permission.REPORTS_PROFIT)

    return QuoteResponse(
        documents=[
            QuoteLine(
                translation_cost=b.translation_cost,
                notary_cost=b.notary_cost,
                certification_cost=b.certification_cost,
                price=b.price,
                translator_cost=b.translator_cost if show_costs else None,
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
        translator_cost_total=result.translator_cost_total if show_costs else None,
        notary_cost_total=result.notary_cost_total,
        gross_profit=result.gross_profit if show_costs else None,
    )
