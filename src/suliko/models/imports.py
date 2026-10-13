"""What a spreadsheet import created, kept so the last one can be undone.

One ``ImportBatch`` per import that saved something, and one item for each
record it created. Undo deletes those records, and only while nothing refers to
them yet. See ``api/v1/imports.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin


class ImportBatch(Base, IdMixin, TenantScoped, TimestampMixin):
    __tablename__ = "import_batches"

    #: "clients" or "translators".
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    rows_total: Mapped[int] = mapped_column(Integer, nullable=False)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Rows skipped because the record was already there.
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Rows skipped because they could not be created.
    error_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    #: Set when the import was undone: its records are gone.
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ImportBatchItem(Base, IdMixin, TenantScoped):
    """One record an import created."""

    __tablename__ = "import_batch_items"
    __table_args__ = (Index("ix_import_batch_items_batch", "batch_id"),)

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False
    )
    #: "client" or "translator".
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
