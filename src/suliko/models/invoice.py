"""The number an order's invoice was given.

Invoices themselves are still computed on every view (see ``api/v1/companies.py``).
What is kept is the one thing that must not change between views: the number
and the day it was issued. The first time an order's invoice is opened a row is
written; every later view, in either language, reads it back.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin


class Invoice(Base, IdMixin, TenantScoped, TimestampMixin):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "order_id", name="uq_invoices_tenant_order"),
        UniqueConstraint("tenant_id", "number", name="uq_invoices_tenant_number"),
    )

    #: Gone with its order.
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    #: Year and month of issue plus a random suffix ("2610-4F7K"): unique in the
    #: bureau, and it does not reveal how many invoices it has issued.
    number: Mapped[str] = mapped_column(String(20), nullable=False)
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
