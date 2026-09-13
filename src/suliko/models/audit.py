"""Audit log.

For a product sold to other companies, the audit trail is part of the product,
not just an operational nicety. The PHP app logs almost nothing.

Append-only: the application role is granted INSERT and SELECT but not UPDATE
or DELETE (enforced in the migration). An audit log the application can edit
is not evidence of anything.
"""

from __future__ import annotations

import enum
from typing import Any

from sqlalchemy import Index, Integer, String
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TimestampMixin


class ActorType(enum.StrEnum):
    USER = "user"
    SUPERUSER = "superuser"
    API_PARTNER = "api_partner"
    SYSTEM = "system"


class AuditLog(Base, IdMixin, TimestampMixin):
    """One immutable record of a consequential action.

    Deliberately NOT ``TenantScoped``: platform-level events (tenant created,
    impersonation started) have no tenant, and the superuser must be able to
    read across tenants. ``tenant_id`` is therefore a plain nullable column,
    and access is gated in the query layer rather than by the ORM filter — the
    one place in the app where that trade is correct, and it is why
    ``audit.read`` is a superuser-only permission.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_tenant_time", "tenant_id", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_actor", "actor_id", "created_at"),
        Index("ix_audit_action", "action", "created_at"),
    )

    tenant_id: Mapped[int | None] = mapped_column(Integer, default=None)

    actor_id: Mapped[int | None] = mapped_column(Integer, default=None)
    actor_type: Mapped[ActorType] = mapped_column(
        String(20), default=ActorType.USER.value, nullable=False
    )
    # Both identities are recorded when a superuser acts through impersonation.
    impersonated_by: Mapped[int | None] = mapped_column(Integer, default=None)

    # Dotted verb: 'order.status_changed', 'payment.recorded', 'user.role_changed'.
    action: Mapped[str] = mapped_column(String(100), nullable=False)

    entity_type: Mapped[str | None] = mapped_column(String(50), default=None)
    entity_id: Mapped[int | None] = mapped_column(Integer, default=None)

    # Secrets are stripped before these are written — see core.audit.redact.
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    ip: Mapped[str | None] = mapped_column(INET, default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
