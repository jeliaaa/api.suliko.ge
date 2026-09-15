"""Notification fan-out and mention parsing.

The bell is the one feature where being slightly wrong is immediately visible
to every user in the office, so the rules that decide who gets an entry are
pinned here.
"""

from __future__ import annotations

from typing import Any

import pytest

from suliko.api.v1.notifications import MENTION_PATTERN
from suliko.api.v1.service_pages import _group_of
from suliko.domain.notifications import notify
from suliko.models.collaboration import Notification, NotificationKind


class FakeSession:
    """Collects what would have been added, without a database."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)


async def _notify(**kwargs: Any) -> tuple[FakeSession, int]:
    db = FakeSession()
    count = await notify(db, **kwargs)  # type: ignore[arg-type]
    return db, count


# ── Fan-out ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_row_per_recipient() -> None:
    db, count = await _notify(user_ids=[1, 2, 3], kind=NotificationKind.COMMENT, body="hello")
    assert count == 3
    assert len(db.added) == 3
    assert all(isinstance(row, Notification) for row in db.added)
    assert {row.user_id for row in db.added} == {1, 2, 3}


@pytest.mark.asyncio
async def test_the_actor_is_never_notified() -> None:
    """Being told you did the thing you just did is noise, and it would make
    the unread badge increment on your own action."""
    db, count = await _notify(
        user_ids=[1, 2, 3],
        kind=NotificationKind.COMMENT,
        body="hello",
        actor_user_id=2,
    )
    assert count == 2
    assert {row.user_id for row in db.added} == {1, 3}


@pytest.mark.asyncio
async def test_duplicate_recipients_collapse() -> None:
    """A user who is both a mentioned party and a thread participant must not
    get the same entry twice."""
    db, count = await _notify(user_ids=[5, 5, 5, 6], kind=NotificationKind.MENTION, body="hello")
    assert count == 2
    assert [row.user_id for row in db.added] == [5, 6]


@pytest.mark.asyncio
async def test_notifying_nobody_writes_nothing() -> None:
    db, count = await _notify(user_ids=[], kind=NotificationKind.SYSTEM, body="hello")
    assert count == 0
    assert db.added == []


@pytest.mark.asyncio
async def test_a_long_body_is_truncated_to_the_column_width() -> None:
    """`body` is VARCHAR(500). Letting a 5000-character comment through would
    raise a database error at flush, after the comment itself was written."""
    db, _ = await _notify(user_ids=[1], kind=NotificationKind.COMMENT, body="x" * 5000)
    assert len(db.added[0].body) == 500


@pytest.mark.asyncio
async def test_the_target_is_carried_through() -> None:
    db, _ = await _notify(
        user_ids=[1],
        kind=NotificationKind.MENTION,
        body="mentioned you",
        order_id=42,
        comment_id=7,
        subject_label="Acme LLC #42",
        actor_name="tako",
    )
    row = db.added[0]
    assert (row.order_id, row.comment_id) == (42, 7)
    assert row.subject_label == "Acme LLC #42"
    assert row.actor_name == "tako"
    # Unread until someone reads it.
    assert row.read_at is None


# ── Mention parsing ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("@tako please check this", ["tako"]),
        ("cc @tako and @nino", ["tako", "nino"]),
        ("@first.last-name_2 hi", ["first.last-name_2"]),
        ("email me at name@example.com", ["example.com"]),
        ("no mentions here", []),
        ("@a", []),  # one character: below the minimum username length
    ],
)
def test_mention_pattern(body: str, expected: list[str]) -> None:
    assert MENTION_PATTERN.findall(body) == expected


def test_an_unknown_name_is_not_a_mention() -> None:
    """The pattern matches text; the router resolves it against real users.
    `@example.com` above is exactly why: it parses, but no such user exists,
    so nothing is notified and the text stays as written."""
    assert MENTION_PATTERN.findall("thanks @nobody") == ["nobody"]


# ── CMS string grouping ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "group"),
    [
        ("nav.services", "nav"),
        ("footer.legal.privacy", "footer"),
        ("tagline", "general"),
        ("", "general"),
    ],
)
def test_group_of(key: str, group: str) -> None:
    assert _group_of(key) == group
