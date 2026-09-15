"""Audit-log writer.

Called from every consequential handler. Deliberately best-effort at the
boundary: an audit write that fails must not take the business operation down
with it, but it must be loud.
"""

from __future__ import annotations

import enum
import json
import math
from collections.abc import Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.core.crypto import redact
from suliko.db.tenancy import bypass_tenant_scope
from suliko.models.audit import ActorType, AuditLog
from suliko.security.sessions import AuthenticatedSession

log = structlog.get_logger()


def json_safe(value: Any) -> Any:
    """``value`` rebuilt from JSON types only, so it can go into a JSONB column.

    Handlers pass model attributes straight into ``before``/``after``, and those
    include dates, Decimals and enums that ``json.dumps`` rejects. The failure
    would not happen here: ``record`` only adds the row, so it would surface at
    commit and turn the whole request into a 500.

    Conversions are fixed rather than left to ``str()``, so the same change is
    always stored the same way: dates and times as ISO 8601, ``Decimal`` as its
    exact string (never a float, which would round money), enums as their value,
    UUIDs as strings, sets as sorted lists. Anything else falls back to
    ``str()`` rather than failing the audit write.
    """
    # Enums first: a StrEnum or IntEnum is also a str or int, and would
    # otherwise be stored as whatever json.dumps makes of the subclass.
    if isinstance(value, enum.Enum):
        return json_safe(value.value)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        # JSONB rejects NaN and Infinity, which json.dumps would happily write.
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes | bytearray | memoryview):
        # Raw bytes have no place in an audit trail; record that they were there.
        return f"<{len(value)} bytes>"
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        return sorted(
            (json_safe(item) for item in value),
            key=lambda item: json.dumps(item, sort_keys=True),
        )
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    return str(value)


async def record(
    db: AsyncSession,
    session: AuthenticatedSession | None,
    *,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    tenant_id: int | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    actor_type: ActorType | None = None,
) -> None:
    """Write one audit entry.

    ``before``/``after`` are made JSON-safe (see ``json_safe``) and then redacted
    before storage — the audit log is read by more people than the database is,
    so a secret leaked into it is worse than one in a table.

    ``actor_type`` is for actors without a CRM session (the suliko.ge portal);
    when omitted it is derived from ``session`` as before.
    """
    try:
        # AuditLog is not TenantScoped (platform events have no tenant), so the
        # bypass keeps the before-flush hook from trying to stamp it.
        with bypass_tenant_scope():
            db.add(
                AuditLog(
                    tenant_id=tenant_id
                    if tenant_id is not None
                    else (session.tenant_id if session else None),
                    actor_id=session.user_id if session else None,
                    actor_type=(
                        actor_type.value
                        if actor_type is not None
                        else ActorType.SUPERUSER.value
                        if session and session.role.value == "superuser"
                        else ActorType.USER.value
                        if session
                        else ActorType.SYSTEM.value
                    ),
                    impersonated_by=session.impersonated_by_user_id if session else None,
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    before=redact(json_safe(before)) if before else None,
                    after=redact(json_safe(after)) if after else None,
                    ip=ip,
                    user_agent=(user_agent or "")[:255] or None,
                )
            )
    except Exception:
        # Never fail the caller's operation because auditing broke, but make
        # sure it is impossible to miss in the logs.
        log.exception("audit_write_failed", action=action, entity_id=entity_id)
