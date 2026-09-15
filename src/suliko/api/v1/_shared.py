"""Pieces shared by the directory routers (clients, translators, notaries).

These three screens are structurally identical — a searchable, filterable,
paginated list plus CRUD — so the parts that would otherwise be copied three
times live here. What stays in each router is only what genuinely differs:
the model, the fields and the domain rules.
"""

from __future__ import annotations

from pydantic import BaseModel


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
