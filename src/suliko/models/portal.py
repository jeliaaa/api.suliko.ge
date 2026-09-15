"""The translator portal: suliko.ge users who work for partner bureaus.

## Why these tables are platform-level

Everything a bureau owns is ``TenantScoped``. These are not, for one reason: a
translator can work for several bureaus at once (N:M), and the portal has to
find out *which* bureaus before it knows any tenant. A tenant-scoped link table
could only be read with a tenant already bound — and under row-level security a
cross-tenant read returns nothing — so that lookup would need
``bypass_tenant_scope()`` in a feature handler, which is exactly what
``db/tenancy.py`` forbids.

So the split is:

- ``portal_translators`` and ``portal_translator_links`` are platform data,
  managed by the suliko.ge admin and read to discover a translator's bureaus.
- Everything *inside* a bureau — its directory row for the translator, orders,
  documents, Drive folders — stays tenant-scoped and is read inside
  ``tenant_scope``, one bureau at a time, with RLS in force.

``personal_orders`` belong to a translator and to no bureau, so they are
platform-level for the plainer reason that there is no tenant to scope them by.
Access to all of it is decided by the portal identity (``api/portal_deps.py``),
never by the ORM filter.
"""

from __future__ import annotations

import enum
from datetime import date

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TimestampMixin, enum_values


class FileKind(enum.StrEnum):
    """Which side of the translation a file is."""

    SOURCE = "source"
    TRANSLATION = "translation"


class PortalTranslator(Base, IdMixin, TimestampMixin):
    """A suliko.ge account the admin has marked as a translator.

    ``external_user_id`` is the .NET backend's user id (an ASP.NET Identity
    GUID). Name and contact details are a snapshot taken when the admin added
    the account: they seed new directory rows and make the admin list readable,
    and are not kept in sync with suliko.ge.
    """

    __tablename__ = "portal_translators"
    __table_args__ = (
        UniqueConstraint("external_user_id", name="uq_portal_translators_external_user_id"),
    )

    external_user_id: Mapped[str] = mapped_column(String(450), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50), default=None)
    email: Mapped[str | None] = mapped_column(String(255), default=None)

    #: Deactivating hides the Orders tab but deletes nothing, so reactivating
    #: restores the links and personal orders exactly as they were.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PortalTranslatorLink(Base, IdMixin, TimestampMixin):
    """One translator working for one bureau: a row of the N:M relation.

    It points at the bureau's own ``translators`` row, so the bureau keeps its
    rates, bank details and history for that person — and documents already
    assigned to that row show up in the portal the moment the link is made.

    ``tenant_id`` is a plain column rather than ``TenantScoped`` (see the module
    docstring). It is never taken from a request: the admin endpoint writes the
    tenant it resolved, and the portal reads it to choose which scope to enter.
    """

    __tablename__ = "portal_translator_links"
    __table_args__ = (
        UniqueConstraint(
            "portal_translator_id", "tenant_id", name="uq_portal_link_translator_tenant"
        ),
        # One suliko.ge account per directory row. Two accounts sharing a row
        # would each see the other's assignments.
        UniqueConstraint("tenant_id", "translator_id", name="uq_portal_link_directory_row"),
        Index("ix_portal_links_tenant", "tenant_id"),
    )

    portal_translator_id: Mapped[int] = mapped_column(
        ForeignKey("portal_translators.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    # CASCADE: deleting the directory row in the CRM removes this bureau from
    # the translator's portal rather than leaving a link to nothing.
    translator_id: Mapped[int] = mapped_column(
        ForeignKey("translators.id", ondelete="CASCADE"), nullable=False
    )


class PersonalOrder(Base, IdMixin, TimestampMixin):
    """An order a translator tracks for themselves, outside any bureau.

    No bureau can see these, and nothing about them reaches a tenant's tables.
    """

    __tablename__ = "personal_orders"
    __table_args__ = (Index("ix_personal_orders_owner_due", "portal_translator_id", "due_date"),)

    # RESTRICT: a translator's own records must not vanish as a side effect.
    # Admins deactivate translators; nothing deletes them.
    portal_translator_id: Mapped[int] = mapped_column(
        ForeignKey("portal_translators.id", ondelete="RESTRICT"), nullable=False
    )
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)


class PersonalOrderLanguagePair(Base, IdMixin, TimestampMixin):
    """A directed pair on a personal order.

    A table rather than a delimited string for the same reason
    ``translator_language_pairs`` is: "orders into Georgian" must be a query,
    not a LIKE scan.
    """

    __tablename__ = "personal_order_language_pairs"
    __table_args__ = (
        UniqueConstraint(
            "personal_order_id",
            "source_language",
            "target_language",
            name="uq_personal_order_pair",
        ),
    )

    personal_order_id: Mapped[int] = mapped_column(
        ForeignKey("personal_orders.id", ondelete="CASCADE"), nullable=False
    )
    source_language: Mapped[str] = mapped_column(String(5), nullable=False)
    target_language: Mapped[str] = mapped_column(String(5), nullable=False)


class PersonalOrderFile(Base, IdMixin, TimestampMixin):
    """A file on a personal order, stored in the database.

    Personal orders have no bureau and therefore no Shared Drive, so the bytes
    live in ``content``. That is the approach suliko.ge takes for translation
    results — minus the expiry — and it puts the files under the database's own
    backups. The size cap is ``Settings.personal_file_max_bytes``.

    ``content`` is deferred and raise-loaded: listing an order's files must never
    pull every file's bytes into memory, and forgetting ``undefer`` should fail
    loudly rather than quietly issue one large query per file.
    """

    __tablename__ = "personal_order_files"
    __table_args__ = (Index("ix_personal_order_files_order_kind", "personal_order_id", "kind"),)

    personal_order_id: Mapped[int] = mapped_column(
        ForeignKey("personal_orders.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[FileKind] = mapped_column(
        Enum(FileKind, name="file_kind", values_callable=enum_values, native_enum=False, length=20),
        nullable=False,
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Hex SHA-256 of ``content``, so a download can be checked against upload.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, deferred=True, deferred_raiseload=True
    )
