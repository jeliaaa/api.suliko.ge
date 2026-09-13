"""Audit-log writer.

Called from every consequential handler. Deliberately best-effort at the
boundary: an audit write that fails must not take the business operation down
with it, but it must be loud.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.core.crypto import redact
from suliko.db.tenancy import bypass_tenant_scope
from suliko.models.audit import ActorType, AuditLog
from suliko.security.sessions import AuthenticatedSession

log = structlog.get_logger()


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
) -> None:
    """Write one audit entry.

    ``before``/``after`` are redacted before storage — the audit log is read by
    more people than the database is, so a secret leaked into it is worse than
    one in a table.
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
                        ActorType.SUPERUSER.value
                        if session and session.role.value == "superuser"
                        else ActorType.USER.value
                        if session
                        else ActorType.SYSTEM.value
                    ),
                    impersonated_by=session.impersonated_by_user_id if session else None,
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    before=redact(before) if before else None,
                    after=redact(after) if after else None,
                    ip=ip,
                    user_agent=(user_agent or "")[:255] or None,
                )
            )
    except Exception:
        # Never fail the caller's operation because auditing broke, but make
        # sure it is impossible to miss in the logs.
        log.exception("audit_write_failed", action=action, entity_id=entity_id)
