"""Canonical order-status definitions.

Port of the PHP's ``includes/statuses.php`` and the twin of
``app.suliko.ge/src/shared/lib/statuses.ts``. All three must agree;
``tests/test_parity.py`` checks this file against the TypeScript one.

The keys are the values actually stored in ``order_status_events.status`` —
including the ``payed`` misspelling and the space-separated values. They are
live production data. Changing a key rewrites history.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from types import MappingProxyType


class StatusTone(enum.StrEnum):
    NEW = "new"
    SUCCESS = "success"
    WARNING = "warning"
    PICKUP = "pickup"
    INFO = "info"
    DANGER = "danger"
    NEUTRAL = "neutral"


@dataclass(frozen=True, slots=True)
class StatusDefinition:
    label: str
    tone: StatusTone


T = StatusTone

STATUS_DEFINITIONS: MappingProxyType[str, StatusDefinition] = MappingProxyType(
    {
        "new": StatusDefinition("New", T.NEW),
        "confirmed": StatusDefinition("Confirmed", T.SUCCESS),
        "rejected": StatusDefinition("Rejected", T.DANGER),
        # Stored misspelling, kept intentionally. Only the label is corrected.
        "payed": StatusDefinition("Paid", T.SUCCESS),
        "being translated": StatusDefinition("Being Translated", T.WARNING),
        "being corrected": StatusDefinition("Being Corrected", T.WARNING),
        "being notarised": StatusDefinition("Being Notarised", T.WARNING),
        "in_progress": StatusDefinition("In Progress", T.WARNING),
        "translated": StatusDefinition("Translated", T.INFO),
        "translated_by_suliko": StatusDefinition("Translated by Suliko (AI)", T.INFO),
        "sent_for_review": StatusDefinition("Sent for Review", T.INFO),
        "sent to the translator": StatusDefinition("Sent to Translator", T.INFO),
        "documents_uploaded": StatusDefinition("Documents Uploaded", T.WARNING),
        "ready_for_pickup_notary": StatusDefinition("Ready for Pickup (Notary)", T.PICKUP),
        "ready_for_pickup_translator": StatusDefinition("Ready for Pickup (Translator)", T.PICKUP),
        "picked_up": StatusDefinition("Picked Up", T.SUCCESS),
        "sent to the client for confirmation": StatusDefinition(
            "Sent to Client (Confirmation)", T.INFO
        ),
        "sent to the client": StatusDefinition("Sent to Client", T.INFO),
        "sent to custom recipient": StatusDefinition("Sent to Custom Recipient", T.INFO),
        "reviewed": StatusDefinition("Reviewed", T.NEW),
        "ready_for_pickup": StatusDefinition("Ready for Pickup", T.PICKUP),
        "completed": StatusDefinition("Completed", T.SUCCESS),
        "cancelled": StatusDefinition("Cancelled", T.NEUTRAL),
    }
)

#: Excluded from every financial aggregate. The PHP excluded only
#: `cancelled`; a rejected order is no more revenue — and no more a debt the
#: client owes — than a cancelled one, so both are out (decided 2026-09-24).
EXCLUDED_FROM_AGGREGATES: frozenset[str] = frozenset({"cancelled", "rejected"})

#: An order in any other status is still open.
CLOSED_STATUSES: frozenset[str] = frozenset({"completed", "cancelled", "rejected"})

INITIAL_STATUS = "new"


def sql_values(statuses: frozenset[str]) -> tuple[str, ...]:
    """A status set as a deterministic tuple, for ``NOT IN (...)``.

    Iterating a frozenset of strings follows the per-process hash seed, so the
    rendered SQL would differ from run to run — harmless to PostgreSQL, but it
    defeats statement caching and makes the SQL-level tests flaky.
    """
    return tuple(sorted(statuses))


def normalise(status: str | None) -> str:
    return (status or "").strip().lower()


def get_label(status: str | None) -> str:
    key = normalise(status)
    definition = STATUS_DEFINITIONS.get(key)
    if definition:
        return definition.label
    if not key:
        return "No Status"
    return key.replace("_", " ").title()


def get_tone(status: str | None) -> StatusTone:
    definition = STATUS_DEFINITIONS.get(normalise(status))
    return definition.tone if definition else StatusTone.NEUTRAL


def is_known(status: str | None) -> bool:
    return normalise(status) in STATUS_DEFINITIONS


def is_closed(status: str | None) -> bool:
    return normalise(status) in CLOSED_STATUSES


def counts_toward_aggregates(status: str | None) -> bool:
    return normalise(status) not in EXCLUDED_FROM_AGGREGATES
