"""The pricing engine.

A faithful port of the PHP's document pricing, which lives in four places in
the original (``add_translation.php``, ``calculator.php``,
``client/ajax/process_order.php``, ``api/orders.php``). Four copies that can
drift; this is the one implementation, and every caller goes through it.

Reference: docs/02-PRODUCT-SPEC.md §6.1.

## Two deliberate changes from the PHP

1. **Decimal, not float.** The PHP computes in IEEE-754 doubles. For a single
   document the error is invisible; across an order with a dozen lines and
   then across a month of reporting it is not. Every value here is ``Decimal``
   and every result is quantised to 2dp with ROUND_HALF_UP, which is what
   ``number_format()`` does and therefore what the existing invoices show.

2. **Multipliers are per-tenant inputs**, not constants. The PHP reads them
   from a shared config file; here they arrive on ``PricingConfig`` so a
   partner bureau can set its own surcharges.

The notary fee tiers, the 1.18 VAT rate and the flat 5 GEL notary fee are
NOT configurable. They are Georgian notary tariffs, the same for every bureau,
and inventing a per-tenant override would invite someone to mis-set them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from suliko.models.order import CopyType, HandoverMethod, Urgency

# ── Fixed Georgian notary tariffs ───────────────────────────────────────────

VAT_RATE = Decimal("1.18")
NOTARY_FLAT_FEE = Decimal("5")

#: Per-page notarisation cost, by page count. Tiers are (max_pages, rate);
#: the last entry is the open-ended top tier.
NOTARY_PAGE_TIERS: tuple[tuple[int | None, Decimal], ...] = (
    (1, Decimal("6")),
    (10, Decimal("4")),
    (50, Decimal("3")),
    (None, Decimal("2")),
)

#: Additional per-page cost when the copy itself is notary-certified.
CERTIFICATION_PAGE_TIERS: tuple[tuple[int | None, Decimal], ...] = (
    (1, Decimal("4")),
    (10, Decimal("2")),
    (50, Decimal("1")),
    (None, Decimal("0.5")),
)

#: Used when a language pair has no configured rate. Matches the PHP's
#: `?? 15` fallback. Reaching this means reference data is incomplete, so
#: `PriceBreakdown.used_fallback_rate` flags it for the caller to surface.
FALLBACK_RATE_PER_PAGE = Decimal("15")

TWO_PLACES = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    """Quantise to 2dp, rounding half away from zero.

    ROUND_HALF_UP is Python's name for "half away from zero", which is what
    PHP's ``number_format`` and ``round`` do. Python's own ``round()`` uses
    banker's rounding and would disagree with the existing invoices on exact
    halves.
    """
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _tier_rate(page_count: int, tiers: tuple[tuple[int | None, Decimal], ...]) -> Decimal:
    for max_pages, rate in tiers:
        if max_pages is None or page_count <= max_pages:
            return rate
    raise AssertionError("tier table must end with an open-ended entry")


@dataclass(frozen=True, slots=True)
class PricingConfig:
    """Per-tenant pricing knobs, from ``TenantSettings``."""

    urgency_multipliers: dict[Urgency, Decimal]
    delivery_fee: Decimal = Decimal("10")
    translator_share: Decimal = Decimal("0.5")

    @classmethod
    def defaults(cls) -> PricingConfig:
        return cls(
            urgency_multipliers={
                Urgency.STANDARD: Decimal("1.0"),
                Urgency.EXPRESS: Decimal("1.5"),
                Urgency.URGENT: Decimal("2.0"),
            }
        )

    def urgency_multiplier(self, urgency: Urgency) -> Decimal:
        return self.urgency_multipliers.get(urgency, Decimal("1.0"))


@dataclass(frozen=True, slots=True)
class DocumentPricingInput:
    page_count: int
    base_rate_per_page: Decimal | None
    document_type_multiplier: Decimal
    copy_type: CopyType
    urgency: Urgency
    #: Overrides ``copy_type.is_notarized`` when set. Staff can notarise a
    #: plain copy, or waive notarisation on a notary copy type.
    is_notarized: bool | None = None


@dataclass(frozen=True, slots=True)
class PriceBreakdown:
    """Every intermediate value, so the UI can show its working.

    The calculator screen and the order builder both display the derivation,
    and support questions about a price are answered from these fields.
    """

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
    used_fallback_rate: bool

    @property
    def profit(self) -> Decimal:
        return money(self.price - self.translator_cost - self.notary_cost)


def price_document(
    doc: DocumentPricingInput, config: PricingConfig | None = None
) -> PriceBreakdown:
    """Price one document.

    >>> from decimal import Decimal
    >>> b = price_document(DocumentPricingInput(
    ...     page_count=1,
    ...     base_rate_per_page=Decimal("40"),
    ...     document_type_multiplier=Decimal("1"),
    ...     copy_type=CopyType.ORIGINAL,
    ...     urgency=Urgency.STANDARD,
    ... ))
    >>> b.price
    Decimal('40.00')
    >>> b.translator_cost
    Decimal('20.00')
    """
    cfg = config or PricingConfig.defaults()

    # The PHP clamps to at least one page; a zero-page document is a UI bug,
    # not a free translation.
    page_count = max(1, doc.page_count)

    used_fallback = doc.base_rate_per_page is None
    base_rate = (
        doc.base_rate_per_page if doc.base_rate_per_page is not None else FALLBACK_RATE_PER_PAGE
    )

    urgency_multiplier = cfg.urgency_multiplier(doc.urgency)

    translation_cost = base_rate * page_count * doc.document_type_multiplier * urgency_multiplier

    is_notarized = doc.is_notarized if doc.is_notarized is not None else doc.copy_type.is_notarized

    notary_cost = Decimal("0")
    certification_cost = Decimal("0")

    if is_notarized:
        per_page = _tier_rate(page_count, NOTARY_PAGE_TIERS)
        notary_cost = (per_page * page_count * VAT_RATE) + NOTARY_FLAT_FEE

        if doc.copy_type is CopyType.NOTARY_CERTIFIED:
            cert_per_page = _tier_rate(page_count, CERTIFICATION_PAGE_TIERS)
            certification_cost = cert_per_page * page_count * VAT_RATE
            notary_cost += certification_cost

    price = translation_cost + notary_cost
    translator_cost = translation_cost * cfg.translator_share

    return PriceBreakdown(
        translation_cost=money(translation_cost),
        notary_cost=money(notary_cost),
        certification_cost=money(certification_cost),
        price=money(price),
        translator_cost=money(translator_cost),
        base_rate_per_page=base_rate,
        document_type_multiplier=doc.document_type_multiplier,
        urgency_multiplier=urgency_multiplier,
        page_count=page_count,
        is_notarized=is_notarized,
        used_fallback_rate=used_fallback,
    )


@dataclass(frozen=True, slots=True)
class OrderQuote:
    documents: tuple[PriceBreakdown, ...]
    documents_total: Decimal
    delivery_cost: Decimal
    total: Decimal
    translator_cost_total: Decimal
    notary_cost_total: Decimal

    @property
    def gross_profit(self) -> Decimal:
        """Before order expenses, which are subtracted downstream."""
        return money(self.total - self.translator_cost_total - self.notary_cost_total)


def quote_order(
    documents: list[DocumentPricingInput],
    handover_method: HandoverMethod,
    config: PricingConfig | None = None,
) -> OrderQuote:
    """Price a whole order, including the courier fee.

    Totals are summed from the already-quantised per-document figures, not
    recomputed from raw values. That way the order total always equals the sum
    of the line items the client can see — summing raw and rounding once can
    leave the total a tetri off the visible lines, which generates support
    tickets even though it is arguably "more correct".
    """
    cfg = config or PricingConfig.defaults()

    breakdowns = tuple(price_document(d, cfg) for d in documents)

    documents_total = sum((b.price for b in breakdowns), Decimal("0"))
    translator_total = sum((b.translator_cost for b in breakdowns), Decimal("0"))
    notary_total = sum((b.notary_cost for b in breakdowns), Decimal("0"))

    delivery_cost = cfg.delivery_fee if handover_method is HandoverMethod.DELIVERY else Decimal("0")

    return OrderQuote(
        documents=breakdowns,
        documents_total=money(documents_total),
        delivery_cost=money(delivery_cost),
        total=money(documents_total + delivery_cost),
        translator_cost_total=money(translator_total),
        notary_cost_total=money(notary_total),
    )
