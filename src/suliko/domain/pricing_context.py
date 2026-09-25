"""A tenant's pricing inputs, loaded once per request.

`domain.pricing` is pure arithmetic; this is the part that reads the tenant's
rows — settings, language-pair rates, document-type multipliers — and hands
them to it. The quote endpoint, order creation, adding a document to an order
and re-pricing on an urgency change all price through here, so the number a
quote shows and the number an order stores cannot come from two different
readings of the settings.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.domain.plans import TenantPlan, pricing_for_plan
from suliko.domain.pricing import (
    DocumentPricingInput,
    PriceBreakdown,
    PricingConfig,
    price_document,
)
from suliko.models.order import CopyType, Urgency
from suliko.models.reference import DocumentType, LanguagePairPrice, TenantSettings

#: Days from order date to the default due date, when a tenant has no
#: settings row. Same as the PHP portal/API and the column defaults.
DEFAULT_DUE_DAYS: dict[Urgency, int] = {
    Urgency.STANDARD: 5,
    Urgency.EXPRESS: 2,
    Urgency.URGENT: 0,
}


@dataclass(frozen=True, slots=True)
class PricingContext:
    config: PricingConfig
    #: (source, target), lower-cased -> active per-page rate.
    rates: dict[tuple[str, str], Decimal]
    #: document_type_id -> multiplier, for the types that were asked about.
    multipliers: dict[int, Decimal]
    due_days: dict[Urgency, int] = field(default_factory=lambda: dict(DEFAULT_DUE_DAYS))

    def rate(self, source: str, target: str) -> Decimal | None:
        return self.rates.get((source.lower(), target.lower()))

    def price(
        self,
        *,
        document_type_id: int,
        source_language: str,
        target_language: str,
        page_count: int,
        copy_type: CopyType,
        urgency: Urgency,
        is_notarized: bool | None = None,
    ) -> PriceBreakdown:
        return price_document(
            DocumentPricingInput(
                page_count=page_count,
                base_rate_per_page=self.rate(source_language, target_language),
                document_type_multiplier=self.multipliers.get(document_type_id, Decimal("1.0")),
                copy_type=copy_type,
                urgency=urgency,
                is_notarized=is_notarized,
            ),
            self.config,
        )


async def tenant_settings(db: AsyncSession) -> TenantSettings | None:
    return (await db.execute(select(TenantSettings))).scalars().first()


def config_from_settings(settings: TenantSettings | None, plan: TenantPlan) -> PricingConfig:
    """Per-tenant multipliers, falling back to the documented defaults, then
    adjusted for the plan (`plans.pricing_for_plan`) on both branches — so a
    tenant with no settings row is not the one case where a freelancer is
    charged a translator share."""
    if settings is None:
        return pricing_for_plan(PricingConfig.defaults(), plan)
    return pricing_for_plan(
        PricingConfig(
            urgency_multipliers={
                Urgency.STANDARD: Decimal(str(settings.urgency_multiplier_standard)),
                Urgency.EXPRESS: Decimal(str(settings.urgency_multiplier_express)),
                Urgency.URGENT: Decimal(str(settings.urgency_multiplier_urgent)),
            },
            delivery_fee=Decimal(str(settings.delivery_fee)),
            translator_share=Decimal(str(settings.default_translator_share)),
        ),
        plan,
    )


def due_days_from_settings(settings: TenantSettings | None) -> dict[Urgency, int]:
    if settings is None:
        return dict(DEFAULT_DUE_DAYS)
    return {
        Urgency.STANDARD: int(settings.due_days_standard),
        Urgency.EXPRESS: int(settings.due_days_express),
        Urgency.URGENT: int(settings.due_days_urgent),
    }


async def load_pricing_context(
    db: AsyncSession, plan: TenantPlan, document_type_ids: Iterable[int] = ()
) -> PricingContext:
    """Everything needed to price documents of the given types.

    Two queries for any number of documents — a 50-document order should not
    be 100 round trips.
    """
    settings = await tenant_settings(db)
    rates = {
        (row.source_language.lower(), row.target_language.lower()): Decimal(str(row.price_per_page))
        for row in (
            await db.execute(select(LanguagePairPrice).where(LanguagePairPrice.is_active))
        ).scalars()
    }
    type_ids = set(document_type_ids)
    multipliers: dict[int, Decimal] = {}
    if type_ids:
        multipliers = {
            row.id: Decimal(str(row.price_multiplier))
            for row in (
                await db.execute(select(DocumentType).where(DocumentType.id.in_(type_ids)))
            ).scalars()
        }
    return PricingContext(
        config=config_from_settings(settings, plan),
        rates=rates,
        multipliers=multipliers,
        due_days=due_days_from_settings(settings),
    )


__all__ = [
    "DEFAULT_DUE_DAYS",
    "PricingContext",
    "config_from_settings",
    "due_days_from_settings",
    "load_pricing_context",
    "tenant_settings",
]
