"""Order files: what a person sees, with the bytes kept in object storage.

Tenant-scoped like everything else a bureau owns. One row per file, attached to
one document — translators are assigned per document and may see only their
own documents' files, so a per-order list could not express that.

## Ids

``public_id`` is what URLs and the frontends carry: random, so nothing can be
enumerated by counting, and shaped like the Drive ids it replaces so the
frontends' route patterns did not have to change. The integer ``id`` never
leaves the API.

## Removing a file

A removal sets ``deleted_at`` and nothing else. The bytes stay put for
``FILE_RETENTION_DAYS`` — the same grace a Drive bin gave — and
``suliko purge-files`` deletes them afterwards. Deleting the document (or its
order) leaves the rows behind with ``order_document_id`` NULL and
``deleted_at`` set by the handler, so the purge finds those too and no object
is ever orphaned in the bucket.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin, enum_values
from suliko.models.portal import FileKind


class OrderFile(Base, IdMixin, TenantScoped, TimestampMixin):
    __tablename__ = "order_files"
    __table_args__ = (Index("ix_order_files_document_kind", "order_document_id", "kind"),)

    public_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    #: NULL once the document is deleted; see the module docstring.
    order_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_documents.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[FileKind] = mapped_column(
        Enum(FileKind, name="file_kind", values_callable=enum_values, native_enum=False, length=20),
        nullable=False,
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Hex SHA-256 of the bytes, taken on upload.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Where the bytes are. Built by the server, never from a request.
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    #: ``user:<id>`` for a CRM upload, ``portal:<suliko.ge user id>`` for one
    #: from the translator portal.
    uploaded_by: Mapped[str] = mapped_column(String(500), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    deleted_by: Mapped[str | None] = mapped_column(String(500), default=None)
