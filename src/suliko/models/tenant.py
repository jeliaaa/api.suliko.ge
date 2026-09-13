"""Tenant and platform-level models.

These are the only business models that are NOT tenant-scoped: a tenant cannot
be inside itself, and the platform superuser sits above all of them.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TimestampMixin, enum_values


class TenantStatus(enum.StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Tenant(Base, IdMixin, TimestampMixin):
    """A partner translation bureau.

    ``slug`` is the human-readable handle used in platform tooling and, if
    subdomain routing is enabled, in the URL. It is a *hint* only: the
    authoritative tenant always comes from the session, and a URL slug that
    disagrees with the session is rejected rather than honoured.
    """

    __tablename__ = "tenants"

    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[TenantStatus] = mapped_column(
        Enum(
            TenantStatus,
            name="tenant_status",
            values_callable=enum_values,
            native_enum=False,
            length=20,
        ),
        default=TenantStatus.TRIAL,
        nullable=False,
    )
    plan: Mapped[str | None] = mapped_column(String(50), default=None)
    locale: Mapped[str] = mapped_column(String(5), default="ka", nullable=False)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def is_usable(self) -> bool:
        """Suspended tenants can be administered by the platform but not used."""
        return self.status in (TenantStatus.ACTIVE, TenantStatus.TRIAL)

    def __repr__(self) -> str:
        return f"<Tenant {self.id} {self.slug!r} {self.status.value}>"
