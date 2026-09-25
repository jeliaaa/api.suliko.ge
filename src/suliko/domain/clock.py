"""What day it is, for a bureau.

Every "today" in this API is a calendar question asked on a bureau's behalf:
an order's default date, whether it is overdue, which period a report or the
finance overview defaults to, the date on an invoice. Answering them with
``datetime.now(UTC).date()`` is wrong for four hours of every Tbilisi night
(UTC+4), and on the first of a month it files the night's work under the
previous month.

Timestamps are still STORED in UTC (TIMESTAMPTZ); only the conversion to a
calendar date goes through here.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from suliko.models.tenant import DEFAULT_TIMEZONE


@lru_cache(maxsize=64)
def zone(name: str | None) -> ZoneInfo:
    """The zone for an IANA name, falling back to the default.

    A bad stored value must not 500 every request the bureau makes, so an
    unknown name degrades to the default rather than raising. (On Windows this
    needs the ``tzdata`` package, which is why it is a hard dependency.)
    """
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def is_valid_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def now_in(tz: str | None, *, now: datetime | None = None) -> datetime:
    """The current moment, as a wall-clock time in ``tz``."""
    return (now or datetime.now(UTC)).astimezone(zone(tz))


def today_in(tz: str | None, *, now: datetime | None = None) -> date:
    """The calendar date it is in ``tz`` right now."""
    return now_in(tz, now=now).date()


def add_days(day: date, days: int) -> date:
    return day + timedelta(days=days)


__all__ = ["add_days", "is_valid_zone", "now_in", "today_in", "zone"]
