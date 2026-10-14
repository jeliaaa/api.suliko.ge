"""Where an interpreting line takes place, and when.

An oral line may say where (an address as text), on which day and at what time.
A written line has none of it. The place is not priced, so it never re-prices.
"""

from __future__ import annotations

from datetime import date, time
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from suliko.api.v1 import companies
from suliko.api.v1 import orders as api
from suliko.domain.pricing import PricingConfig
from suliko.domain.pricing_context import PricingContext
from suliko.models.order import Urgency


def _ctx() -> PricingContext:
    from decimal import Decimal

    return PricingContext(
        config=PricingConfig.defaults(),
        rates={("ka", "en"): Decimal("20")},
        minute_rates={("ka", "en"): Decimal("2")},
        multipliers={1: Decimal("1.0")},
        characters_per_page=1800,
    )


def _order() -> Any:
    return SimpleNamespace(id=7, urgency=Urgency.STANDARD)


def _oral(**fields: object) -> api.OrderDocumentIn:
    return api.OrderDocumentIn(
        **{
            "service_type": "oral",
            "source_language": "ka",
            "target_language": "en",
            "page_count": 30,
            **fields,
        }  # type: ignore[arg-type]
    )


def test_an_oral_line_may_say_where_and_when() -> None:
    line = _oral(
        interpreting_place="  Tbilisi City Court, Hall 3 ",
        interpreting_date=date(2026, 10, 20),
        interpreting_time=time(14, 30),
    )

    assert line.interpreting_place == "Tbilisi City Court, Hall 3"
    assert line.interpreting_date == date(2026, 10, 20)
    assert line.interpreting_time == time(14, 30)


def test_a_blank_place_is_no_place() -> None:
    assert _oral(interpreting_place="   ").interpreting_place is None


def test_everything_about_the_place_is_optional() -> None:
    line = _oral()

    assert (line.interpreting_place, line.interpreting_date, line.interpreting_time) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    "extra",
    [
        {"interpreting_place": "Tbilisi"},
        {"interpreting_date": date(2026, 10, 20)},
        {"interpreting_time": time(9, 0)},
    ],
)
def test_a_written_line_has_no_place(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="interpreting line"):
        api.OrderDocumentIn(
            **{
                "document_type_id": 1,
                "source_language": "ka",
                "target_language": "en",
                "page_count": 3,
                **extra,
            }  # type: ignore[arg-type]
        )


async def test_the_place_is_kept_on_the_row_and_does_not_change_the_price() -> None:
    with_place = await api._document_row(
        _order(),
        _oral(interpreting_place="Tbilisi City Court", interpreting_date=date(2026, 10, 20)),
        _ctx(),
        None,
    )
    without = await api._document_row(_order(), _oral(), _ctx(), None)

    assert with_place.interpreting_place == "Tbilisi City Court"
    assert with_place.interpreting_date == date(2026, 10, 20)
    assert with_place.interpreting_time is None
    assert with_place.price == without.price


def test_a_change_may_set_or_clear_the_place() -> None:
    change = api.OrderDocumentUpdate(interpreting_place=None)
    assert change.model_dump(exclude_unset=True) == {"interpreting_place": None}
    assert api.OrderDocumentUpdate().model_dump(exclude_unset=True) == {}


def _line(**fields: object) -> Any:
    return SimpleNamespace(
        **{
            "interpreting_place": None,
            "interpreting_date": None,
            "interpreting_time": None,
            **fields,
        }
    )


def test_the_invoice_names_the_place_and_the_time() -> None:
    line = _line(
        interpreting_place="Tbilisi City Court",
        interpreting_date=date(2026, 10, 20),
        interpreting_time=time(9, 5),
    )

    assert companies._oral_description(line, "en") == (
        "Oral interpreting (Tbilisi City Court, 20.10.2026 09:05)"
    )
    assert companies._oral_description(line, "ka").startswith("ზეპირი თარგმანი (Tbilisi City Court")


def test_the_invoice_says_only_what_the_line_says() -> None:
    assert companies._oral_description(_line(), "en") == "Oral interpreting"
    assert companies._oral_description(_line(interpreting_place="Hospital"), "en") == (
        "Oral interpreting (Hospital)"
    )
    # A time with no day is not printed: it would say nothing.
    assert companies._oral_description(_line(interpreting_time=time(9, 0)), "en") == (
        "Oral interpreting"
    )
