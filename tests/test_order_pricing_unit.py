"""An order is calculated by pages or by characters, and the caller chooses.

The bureau's setting is only where a new order's form starts. The API prices a
line from its character count when it has one and from its pages when it has
not, whatever the setting says; there is no "this bureau must count
characters" refusal any more.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from suliko.api.v1 import orders as api
from suliko.domain.pricing import PricingConfig
from suliko.domain.pricing_context import PricingContext
from suliko.models.order import ServiceType, Urgency


def _ctx() -> PricingContext:
    return PricingContext(
        config=PricingConfig.defaults(),
        rates={("ka", "en"): Decimal("20")},
        minute_rates={("ka", "en"): Decimal("2")},
        multipliers={1: Decimal("1.0")},
        characters_per_page=1800,
    )


def _order() -> Any:
    return SimpleNamespace(id=7, urgency=Urgency.STANDARD)


def _line(**fields: object) -> api.OrderDocumentIn:
    return api.OrderDocumentIn(
        **{
            "document_type_id": 1,
            "source_language": "ka",
            "target_language": "en",
            "page_count": 3,
            **fields,
        }  # type: ignore[arg-type]
    )


async def test_a_line_without_characters_is_priced_by_pages() -> None:
    row = await api._document_row(_order(), _line(), _ctx(), None)
    assert row.character_count is None
    assert row.price == Decimal("60.00")


async def test_a_line_with_characters_is_priced_by_standard_pages() -> None:
    row = await api._document_row(_order(), _line(character_count=3600), _ctx(), None)
    # 3600 characters at 1800 to a page: two standard pages, not the three
    # physical ones. The physical pages stay for the notary.
    assert row.character_count == 3600
    assert row.page_count == 3
    assert row.price == Decimal("40.00")


async def test_a_short_text_counts_as_one_standard_page() -> None:
    row = await api._document_row(_order(), _line(character_count=120), _ctx(), None)
    assert row.price == Decimal("20.00")


async def test_an_oral_line_has_no_characters() -> None:
    row = await api._document_row(
        _order(),
        _line(service_type=ServiceType.ORAL, document_type_id=None, character_count=5000),
        _ctx(),
        None,
    )
    assert row.character_count is None
    assert row.price == Decimal("6.00")
