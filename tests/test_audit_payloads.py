"""Audit payloads must survive the trip into a JSONB column.

Handlers pass model attributes straight into ``record(before=..., after=...)``:
order due dates, translator rates as ``Decimal``, enums. Plain ``json.dumps``
rejects the first two, and because ``record`` only adds the row, the error
surfaced at commit — as a 500 on the very request being audited. The suite never
saw it: SQLite cannot create ``audit_log``, so no test wrote a real audit row.

These run the conversion through the actual PostgreSQL/asyncpg JSONB bind
processor, which is the step that failed. No database is needed.
"""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql.asyncpg import dialect as asyncpg_dialect

from suliko.core.audit import json_safe, record
from suliko.models.audit import AuditLog
from suliko.models.order import Urgency


class Priority(enum.IntEnum):
    LOW = 1
    HIGH = 2


def _bind_as_jsonb(value: Any) -> Any:
    """What SQLAlchemy does to a JSONB value on its way to asyncpg."""
    processor = JSONB().bind_processor(asyncpg_dialect())
    assert processor is not None
    return processor(value)


PAYLOAD: dict[str, Any] = {
    "due_date": date(2026, 9, 20),
    "changed_at": datetime(2026, 9, 15, 12, 30, tzinfo=UTC),
    "opens_at": time(9, 0),
    "default_rate": Decimal("40.50"),
    "urgency": Urgency.EXPRESS,
    "priority": Priority.HIGH,
    "request_id": UUID("12345678-1234-5678-1234-567812345678"),
    "nested": {"amounts": [Decimal("1.10"), Decimal("2.20")], "when": (date(2026, 1, 1),)},
    "tags": {"b", "a"},
    "ratio": float("nan"),
    "blob": b"\x00\x01",
    "plain": {"flag": True, "missing": None, "count": 3, "name": "x", "share": 0.5},
}


def test_the_unconverted_payload_is_what_broke() -> None:
    """Pins the failure being fixed, so a regression in the fix is visible."""
    with pytest.raises(TypeError, match="not JSON serializable"):
        _bind_as_jsonb({"due_date": date(2026, 9, 20)})
    with pytest.raises(TypeError, match="not JSON serializable"):
        _bind_as_jsonb({"default_rate": Decimal("40.50")})


def test_converted_payload_binds_to_jsonb() -> None:
    _bind_as_jsonb(json_safe(PAYLOAD))


def test_conversions_are_exact_and_stable() -> None:
    assert json_safe(PAYLOAD) == {
        "due_date": "2026-09-20",
        "changed_at": "2026-09-15T12:30:00+00:00",
        "opens_at": "09:00:00",
        # The exact string, never a float: 40.50 must not become 40.5 or 40.4999…
        "default_rate": "40.50",
        "urgency": "express",
        "priority": 2,
        "request_id": "12345678-1234-5678-1234-567812345678",
        "nested": {"amounts": ["1.10", "2.20"], "when": ["2026-01-01"]},
        # Sets have no order; stored sorted so the same change reads the same.
        "tags": ["a", "b"],
        # JSONB rejects NaN, which json.dumps would otherwise emit.
        "ratio": "nan",
        "blob": "<2 bytes>",
        "plain": {"flag": True, "missing": None, "count": 3, "name": "x", "share": 0.5},
    }


def test_unknown_objects_fall_back_to_their_string_form() -> None:
    class Thing:
        def __str__(self) -> str:
            return "a thing"

    assert json_safe({"value": Thing()}) == {"value": "a thing"}


class _CapturingSession:
    """Stands in for an AsyncSession: ``record`` only ever calls ``add``."""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, instance: object) -> None:
        self.added.append(instance)


async def test_record_stores_jsonb_safe_and_redacted_values() -> None:
    """The shapes update_order and update_translator really pass."""
    db = _CapturingSession()
    await record(
        db,  # type: ignore[arg-type]
        None,
        action="order.updated",
        entity_type="order",
        entity_id=101,
        before={"due_date": date(2026, 9, 20), "password_hash": "$argon2id$..."},
        after={"due_date": date(2026, 9, 27), "default_rate": Decimal("45.00")},
    )

    [entry] = db.added
    assert isinstance(entry, AuditLog)
    assert entry.before == {"due_date": "2026-09-20", "password_hash": "[redacted]"}
    assert entry.after == {"due_date": "2026-09-27", "default_rate": "45.00"}
    _bind_as_jsonb(entry.before)
    _bind_as_jsonb(entry.after)
