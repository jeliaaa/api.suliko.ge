"""Pricing engine tests.

These are the most important tests in the repo. The formula was reverse-
engineered from PHP that runs a real business; if it is wrong, every invoice
is wrong, and nobody notices until a translator or a client disputes a figure.

The tier boundaries (1, 2, 10, 11, 50, 51) are tested exhaustively because
off-by-one in a tier table is the classic failure and is invisible in
spot-checks.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from suliko.domain.pricing import (
    CERTIFICATION_PAGE_TIERS,
    FALLBACK_RATE_PER_PAGE,
    NOTARY_FLAT_FEE,
    NOTARY_PAGE_TIERS,
    VAT_RATE,
    DocumentPricingInput,
    PricingConfig,
    price_document,
    quote_order,
)
from suliko.models.order import CopyType, HandoverMethod, Urgency

D = Decimal


def doc(
    pages: int = 1,
    rate: str | None = "40",
    multiplier: str = "1",
    copy_type: CopyType = CopyType.ORIGINAL,
    urgency: Urgency = Urgency.STANDARD,
) -> DocumentPricingInput:
    return DocumentPricingInput(
        page_count=pages,
        base_rate_per_page=D(rate) if rate is not None else None,
        document_type_multiplier=D(multiplier),
        copy_type=copy_type,
        urgency=urgency,
    )


# ── The base formula ────────────────────────────────────────────────────────


def test_simple_translation() -> None:
    b = price_document(doc(pages=1, rate="40"))
    assert b.translation_cost == D("40.00")
    assert b.notary_cost == D("0.00")
    assert b.price == D("40.00")
    assert b.translator_cost == D("20.00")  # 50% default share


def test_price_scales_with_pages() -> None:
    assert price_document(doc(pages=3, rate="40")).price == D("120.00")


def test_document_type_multiplier_applies() -> None:
    assert price_document(doc(pages=1, rate="40", multiplier="1.5")).price == D("60.00")


@pytest.mark.parametrize(
    ("urgency", "expected"),
    [
        (Urgency.STANDARD, "40.00"),
        (Urgency.EXPRESS, "60.00"),  # +50%
        (Urgency.URGENT, "80.00"),  # +100%
    ],
)
def test_urgency_multipliers(urgency: Urgency, expected: str) -> None:
    assert price_document(doc(rate="40", urgency=urgency)).price == D(expected)


def test_multipliers_compound() -> None:
    # 40 * 2 pages * 1.5 type * 2.0 urgent
    b = price_document(doc(pages=2, rate="40", multiplier="1.5", urgency=Urgency.URGENT))
    assert b.price == D("240.00")


def test_zero_pages_is_clamped_to_one() -> None:
    """A zero-page document is a UI bug, not a free translation."""
    assert price_document(doc(pages=0, rate="40")).price == D("40.00")


def test_missing_rate_falls_back_and_flags() -> None:
    b = price_document(doc(rate=None))
    assert b.base_rate_per_page == FALLBACK_RATE_PER_PAGE
    assert b.used_fallback_rate is True, "caller must be able to surface incomplete pricing data"


# ── Notary tiers ────────────────────────────────────────────────────────────


def _expected_notary(pages: int, per_page: str) -> Decimal:
    return (D(per_page) * pages * VAT_RATE + NOTARY_FLAT_FEE).quantize(D("0.01"))


@pytest.mark.parametrize(
    ("pages", "per_page"),
    [
        (1, "6"),  # tier 1
        (2, "4"),  # first page of tier 2..10
        (10, "4"),  # last page of tier 2..10
        (11, "3"),  # first page of tier 11..50
        (50, "3"),  # last page of tier 11..50
        (51, "2"),  # open-ended top tier
        (200, "2"),
    ],
)
def test_notary_tier_boundaries(pages: int, per_page: str) -> None:
    b = price_document(doc(pages=pages, rate="40", copy_type=CopyType.NOTARY_ORIGINAL))
    assert b.notary_cost == _expected_notary(pages, per_page)


def test_one_page_notarised_matches_production() -> None:
    """1 * 6 * 1.18 + 5 = 12.08.

    Order #1140 in the production screenshots is a single-page notarised
    document priced at 52.08 GEL, which is 40.00 translation + 12.08 notary.
    Consistent with a 40/page rate and this formula.
    """
    b = price_document(doc(pages=1, rate="40", copy_type=CopyType.NOTARY_ORIGINAL))
    assert b.notary_cost == D("12.08")
    assert b.price == D("52.08")


@pytest.mark.parametrize(
    "copy_type",
    [CopyType.NOTARY_ORIGINAL, CopyType.NOTARY_COPY, CopyType.NOTARY_CERTIFIED],
)
def test_notary_copy_types_are_notarised(copy_type: CopyType) -> None:
    assert copy_type.is_notarized
    assert price_document(doc(copy_type=copy_type)).notary_cost > 0


@pytest.mark.parametrize("copy_type", [CopyType.ORIGINAL, CopyType.PLAIN])
def test_plain_copy_types_are_not_notarised(copy_type: CopyType) -> None:
    assert not copy_type.is_notarized
    assert price_document(doc(copy_type=copy_type)).notary_cost == D("0.00")


# ── Certification surcharge ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pages", "cert_per_page"),
    [(1, "4"), (2, "2"), (10, "2"), (11, "1"), (50, "1"), (51, "0.5")],
)
def test_certification_tiers(pages: int, cert_per_page: str) -> None:
    certified = price_document(doc(pages=pages, rate="40", copy_type=CopyType.NOTARY_CERTIFIED))
    plain_notary = price_document(doc(pages=pages, rate="40", copy_type=CopyType.NOTARY_ORIGINAL))

    expected_surcharge = (D(cert_per_page) * pages * VAT_RATE).quantize(D("0.01"))
    assert certified.certification_cost == expected_surcharge
    # The surcharge is additive on top of the ordinary notary cost.
    assert certified.notary_cost - plain_notary.notary_cost == expected_surcharge


def test_certification_only_applies_to_certified_copies() -> None:
    b = price_document(doc(copy_type=CopyType.NOTARY_ORIGINAL))
    assert b.certification_cost == D("0.00")


# ── Overrides ───────────────────────────────────────────────────────────────


def test_explicit_notarised_flag_overrides_copy_type() -> None:
    """Staff can notarise a plain copy, or waive it on a notary copy type."""
    forced_on = price_document(
        DocumentPricingInput(
            page_count=1,
            base_rate_per_page=D("40"),
            document_type_multiplier=D("1"),
            copy_type=CopyType.PLAIN,
            urgency=Urgency.STANDARD,
            is_notarized=True,
        )
    )
    assert forced_on.notary_cost == D("12.08")

    forced_off = price_document(
        DocumentPricingInput(
            page_count=1,
            base_rate_per_page=D("40"),
            document_type_multiplier=D("1"),
            copy_type=CopyType.NOTARY_ORIGINAL,
            urgency=Urgency.STANDARD,
            is_notarized=False,
        )
    )
    assert forced_off.notary_cost == D("0.00")


def test_translator_share_is_configurable() -> None:
    cfg = PricingConfig(
        urgency_multipliers=PricingConfig.defaults().urgency_multipliers,
        translator_share=D("0.6"),
    )
    assert price_document(doc(rate="100"), cfg).translator_cost == D("60.00")


def test_tenant_can_override_urgency_multipliers() -> None:
    cfg = PricingConfig(
        urgency_multipliers={
            Urgency.STANDARD: D("1.0"),
            Urgency.EXPRESS: D("1.25"),
            Urgency.URGENT: D("3.0"),
        }
    )
    assert price_document(doc(rate="40", urgency=Urgency.EXPRESS), cfg).price == D("50.00")
    assert price_document(doc(rate="40", urgency=Urgency.URGENT), cfg).price == D("120.00")


# ── Order-level quoting ─────────────────────────────────────────────────────


def test_order_total_is_sum_of_documents() -> None:
    q = quote_order(
        [doc(pages=1, rate="40"), doc(pages=2, rate="45")],
        HandoverMethod.SCAN,
    )
    assert q.documents_total == D("130.00")
    assert q.delivery_cost == D("0.00")
    assert q.total == D("130.00")


def test_courier_delivery_adds_the_fee() -> None:
    q = quote_order([doc(pages=1, rate="40")], HandoverMethod.DELIVERY)
    assert q.delivery_cost == D("10.00")
    assert q.total == D("50.00")


@pytest.mark.parametrize("method", [HandoverMethod.SCAN, HandoverMethod.PICKUP])
def test_scan_and_pickup_are_free(method: HandoverMethod) -> None:
    assert quote_order([doc(rate="40")], method).delivery_cost == D("0.00")


def test_order_total_equals_visible_line_items() -> None:
    """The total must equal the sum of the displayed lines, exactly.

    Summing raw values and rounding once can leave the total a tetri away from
    the lines the client can see. That difference is arguably more accurate
    and is definitely a support ticket, so the engine sums the rounded lines.
    """
    documents = [doc(pages=3, rate="33.33"), doc(pages=7, rate="11.11")]
    q = quote_order(documents, HandoverMethod.SCAN)
    assert q.documents_total == sum(b.price for b in q.documents)


def test_gross_profit_excludes_expenses() -> None:
    q = quote_order(
        [doc(pages=1, rate="40", copy_type=CopyType.NOTARY_ORIGINAL)], HandoverMethod.SCAN
    )
    # price 52.08 - translator 20.00 - notary 12.08
    assert q.gross_profit == D("20.00")


# ── Money semantics ─────────────────────────────────────────────────────────


def test_everything_is_decimal_never_float() -> None:
    b = price_document(doc(pages=3, rate="33.33"))
    for value in (b.price, b.translation_cost, b.notary_cost, b.translator_cost):
        assert isinstance(value, Decimal)


def test_results_are_quantised_to_two_places() -> None:
    b = price_document(doc(pages=7, rate="11.11", copy_type=CopyType.NOTARY_CERTIFIED))
    for value in (b.price, b.translation_cost, b.notary_cost, b.translator_cost):
        assert value.as_tuple().exponent == -2


def test_rounds_half_away_from_zero_not_bankers() -> None:
    """Python's round() would give 0.12 for 0.125; PHP's number_format gives 0.13.

    Every existing invoice was produced by the PHP, so matching it matters
    more than matching Python's default.
    """
    # 0.25 * 1 page * 1.0 * 1.0 -> translator share 0.5 -> 0.125 -> 0.13
    b = price_document(doc(pages=1, rate="0.25"))
    assert b.translator_cost == D("0.13")


def test_no_float_contamination_in_large_orders() -> None:
    """100 lines of 0.1 must total exactly 10.00, which floats would not."""
    q = quote_order([doc(pages=1, rate="0.10") for _ in range(100)], HandoverMethod.SCAN)
    assert q.documents_total == D("10.00")


# ── Tier tables are well-formed ─────────────────────────────────────────────


@pytest.mark.parametrize("tiers", [NOTARY_PAGE_TIERS, CERTIFICATION_PAGE_TIERS])
def test_tier_tables_end_open_ended(tiers: tuple[tuple[int | None, Decimal], ...]) -> None:
    """A closed final tier would raise for large page counts."""
    assert tiers[-1][0] is None
    assert all(max_pages is not None for max_pages, _ in tiers[:-1])


@pytest.mark.parametrize("tiers", [NOTARY_PAGE_TIERS, CERTIFICATION_PAGE_TIERS])
def test_tier_rates_decrease(tiers: tuple[tuple[int | None, Decimal], ...]) -> None:
    """Bulk work gets cheaper per page — an increase would be a transcription error."""
    rates = [rate for _, rate in tiers]
    assert rates == sorted(rates, reverse=True)
