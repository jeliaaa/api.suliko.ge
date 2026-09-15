"""A bureau's Google Shared Drive, and the order folders Suliko keeps in it.

Tenant-scoped like everything else a bureau owns: the drive is the bureau's,
and so are the folders for its orders. These live in their own tables rather
than as columns on ``tenant_settings``, ``orders`` and ``order_documents`` for a
migration reason — revision 0001 builds its tables from metadata, so a column
added to one of those models would be created by 0001 on a fresh database and
then fail when a later revision adds it again.

Folder ids are recorded on first use. Renaming a folder in Drive therefore
breaks nothing, and a folder deleted in Drive is simply created again the next
time it is needed.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin


class DriveSettings(Base, IdMixin, TenantScoped, TimestampMixin):
    """Which Shared Drive this bureau's order files go to. One row per tenant.

    The drive id is not a credential: access comes from the bureau adding
    Suliko's service account to the drive, and can be withdrawn the same way.
    """

    __tablename__ = "drive_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_drive_settings_tenant"),)

    shared_drive_id: Mapped[str] = mapped_column(String(100), nullable=False)
    #: The drive's name when it was linked, shown in the admin panel so a
    #: mistyped id is noticed as "that's not our drive".
    drive_name: Mapped[str | None] = mapped_column(String(255), default=None)


class OrderDriveFolder(Base, IdMixin, TenantScoped, TimestampMixin):
    """The folder for one order: ``Suliko Orders/#123 · Client``."""

    __tablename__ = "order_drive_folders"
    __table_args__ = (UniqueConstraint("order_id", name="uq_order_drive_folders_order"),)

    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    folder_id: Mapped[str] = mapped_column(String(100), nullable=False)


class OrderDocumentDriveFolder(Base, IdMixin, TenantScoped, TimestampMixin):
    """The folders for one document: its own folder, and Source/Translation in it.

    Per document, not per order, because translators are assigned per document
    and may only see their own documents' files — one shared order folder could
    not express that.
    """

    __tablename__ = "order_document_drive_folders"
    __table_args__ = (
        UniqueConstraint("order_document_id", name="uq_order_document_drive_folders_document"),
    )

    order_document_id: Mapped[int] = mapped_column(
        ForeignKey("order_documents.id", ondelete="CASCADE"), nullable=False
    )
    folder_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source_folder_id: Mapped[str] = mapped_column(String(100), nullable=False)
    translation_folder_id: Mapped[str] = mapped_column(String(100), nullable=False)
