"""A translator's language pairs run both ways.

A translator who works from Georgian into English is assumed to work from
English into Georgian too. Storing one row per direction (as the table always
has) keeps every query as it was: the list filter, the order form's soft
check and the profile all read directions. What changed is who writes them:
a pair that is saved gets its reverse saved with it, unless it is marked
``one_way`` (the rare translator who only goes one way).

Rates stay per direction. A reverse that has no rate of its own is stored
empty, and an empty rate means "the one that is set": the screens show the
reverse as the forward direction's rate until someone types a different one.

Pure: no database, so the rule is testable by itself.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Pair:
    """One direction: from `source` into `target`, with an optional own rate."""

    source: str
    target: str
    rate: Decimal | None = None
    #: Keep it to this one direction: do not add the reverse.
    one_way: bool = False


def both_ways(pairs: Iterable[Pair]) -> list[Pair]:
    """The pairs as they are stored: each followed by its reverse.

    The reverse is added with no rate of its own, unless it is one way or the
    caller listed the reverse itself (then that listing, with its own rate,
    stands and nothing is added). The listed order is kept, each added reverse
    sitting right after the pair it came from, so a profile reads
    KA to EN, EN to KA, KA to DE, DE to KA.
    """
    listed = list(pairs)
    explicit = {(p.source, p.target) for p in listed}
    stored: list[Pair] = []
    for pair in listed:
        stored.append(Pair(pair.source, pair.target, pair.rate))
        reverse = (pair.target, pair.source)
        if not pair.one_way and reverse not in explicit:
            explicit.add(reverse)
            stored.append(Pair(pair.target, pair.source))
    return stored
