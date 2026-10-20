"""The month a monthly invoice covers.

An order delivered at 01:00 on the 1st, Tbilisi time, is still the previous
day in UTC. Filing it under the wrong month would bill a client twice or not
at all, so the bounds follow the bureau's calendar, not UTC's.
"""

from __future__ import annotations

from datetime import UTC, datetime

from suliko.api.v1.monthly_invoice import _month_bounds


def test_the_month_starts_at_local_midnight() -> None:
    start, end = _month_bounds("2026-10", "Asia/Tbilisi")
    assert start == datetime(2026, 9, 30, 20, 0, tzinfo=UTC)
    assert end == datetime(2026, 10, 31, 20, 0, tzinfo=UTC)


def test_a_night_delivery_on_the_first_belongs_to_the_new_month() -> None:
    start, _ = _month_bounds("2026-10", "Asia/Tbilisi")
    delivered = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)  # 01:00 on 1 Oct, Tbilisi
    assert delivered >= start


def test_december_rolls_into_the_next_year() -> None:
    _, end = _month_bounds("2026-12", "Asia/Tbilisi")
    assert end == datetime(2026, 12, 31, 20, 0, tzinfo=UTC)
