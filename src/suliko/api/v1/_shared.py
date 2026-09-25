"""Pieces shared by the directory routers (clients, translators, notaries).

These three screens are structurally identical — a searchable, filterable,
paginated list plus CRUD — so the parts that would otherwise be copied three
times live here. What stays in each router is only what genuinely differs:
the model, the fields and the domain rules.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.sql import ColumnElement


class PageMeta(BaseModel):
    """Drives the "Showing 20 of 688" counter every list screen carries."""

    total: int
    limit: int
    offset: int


def mask_tail(value: str | None, visible: int = 4) -> str | None:
    """Mask all but the last few characters.

    Used for national ID numbers and IBANs in list responses. These are the
    fields that end up in screenshots, exported spreadsheets and support
    tickets; the full value stays behind the detail endpoint.

    Returns None for None so a missing value renders as an em dash rather than
    a row of dots suggesting something is there.
    """
    if not value:
        return None
    if len(value) <= visible:
        return "•" * len(value)
    return f"{'•' * (len(value) - visible)}{value[-visible:]}"


# ── Money fields ────────────────────────────────────────────────────────────
#
# Every amount column is NUMERIC(10, 2). A bare `Decimal` field accepts
# 0.004 (passes `gt=0`, is stored as 0.00, then fails the database's
# `amount > 0` CHECK as a 500) and 123456789.00 (overflows the column). These
# say what the column can hold, so the caller gets a 422 naming the field.

#: A strictly positive amount: payments, payouts, expenses, allocations.
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]
#: Zero allowed: prices and costs, where a free job or an absorbed fee is real.
Money = Annotated[Decimal, Field(ge=0, max_digits=10, decimal_places=2)]


# ── Search ──────────────────────────────────────────────────────────────────

_LIKE_ESCAPE = "\\"


def like_pattern(search: str) -> str:
    """A `%…%` pattern that matches the text literally.

    `%` and `_` in what somebody typed are wildcards to LIKE; unescaped, a
    search for "50%" matches everything containing "50". Use with
    ``ilike(pattern, escape=LIKE_ESCAPE)``.
    """
    escaped = (
        search.strip()
        .replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


LIKE_ESCAPE = _LIKE_ESCAPE


def digits_of(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def phone_digits(column: ColumnElement[str | None] | Any) -> ColumnElement[str | None]:
    """A phone column with the usual separators stripped, portably.

    Phones are stored as typed — "+995 555 12-34-56", "555123456" — so a
    literal LIKE only finds a number typed the same way twice. Nested
    `replace` rather than `regexp_replace` so it also runs on SQLite, which
    the tests use.
    """
    stripped: ColumnElement[str | None] = column
    for ch in (" ", "-", "(", ")", "+", "."):
        stripped = func.replace(stripped, ch, "")
    return stripped
