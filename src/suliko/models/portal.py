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

``portal_account_invites`` is platform-level for the same reason as
``portal_translator_links``: resolving one means finding out, later and for an
account nobody has identified yet, that it now matches an invite some bureau
wrote — a lookup that has to run before any tenant is bound. See
``domain.portal.account_matches`` and ``domain.portal.resolve_pending_invites``.
"""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
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


class InviteKind(enum.StrEnum):
    """What being invited turns into once it resolves.

    ``TRANSLATOR`` links to a bureau's ``translators`` directory row —
    ``PortalTranslatorLink``, the same table the suliko.ge admin's manual
    linking writes. ``STAFF`` links to a ``users`` row, which has no portal
    relationship today; resolving it only records which suliko.ge account the
    address belongs to, for the day that changes.
    """

    TRANSLATOR = "translator"
    STAFF = "staff"


class InviteStatus(enum.StrEnum):
    PENDING = "pending"
    LINKED = "linked"
    CANCELLED = "cancelled"


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


class PortalAccountInvite(Base, IdMixin, TimestampMixin):
    """A bureau's claim that one address belongs to a suliko.ge account.

    Written the moment a bureau invites someone — a translator or a staff
    member — by email or phone. If exactly one ``PortalTranslator`` matches at
    that moment, the invite resolves immediately (``LINKED``). Otherwise it
    waits as ``PENDING`` until a matching account exists: either a suliko.ge
    admin marks one as a translator (``portal_admin.upsert_translator`` calls
    ``resolve_pending_invites`` right after), or the translator themselves
    opens the portal (``portal.get_me`` calls it too). Neither path needs a
    scheduler — resolution is a side effect of the two moments a matching
    account can newly exist or newly show up.

    Platform-level for the reason the module docstring gives: it must be
    searchable by contact details across every bureau before any tenant is
    known, which is what makes the ``normalized_*`` columns worth indexing —
    unlike ``domain.portal.account_matches`` (admin-triggered, rare),
    resolution runs on every portal sign-in.

    ``tenant_id`` is a plain column, written from the inviter's session and
    never taken from the request, exactly like ``PortalTranslatorLink.tenant_id``.
    """

    __tablename__ = "portal_account_invites"
    __table_args__ = (
        # Re-inviting the same address updates this row rather than piling up
        # duplicates that would all try to resolve at once.
        UniqueConstraint(
            "tenant_id", "kind", "email", name="uq_portal_account_invites_tenant_kind_email"
        ),
        # A directory row can be the target of at most one invite, matching
        # the one-account-per-row rule `PortalTranslatorLink` already enforces.
        UniqueConstraint("translator_id", name="uq_portal_account_invites_translator_id"),
        Index("ix_portal_account_invites_tenant_status", "tenant_id", "status"),
        Index("ix_portal_account_invites_normalized_email", "normalized_email"),
        Index("ix_portal_account_invites_normalized_phone", "normalized_phone"),
    )

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[InviteKind] = mapped_column(
        Enum(
            InviteKind,
            name="portal_invite_kind",
            values_callable=enum_values,
            native_enum=False,
            length=20,
        ),
        nullable=False,
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: As the bureau typed it, lowercased. Free text, same as `Translator.email`.
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50), default=None)

    #: `domain.portal.normalize_email` / `normalize_phone`, kept in step with
    #: `email`/`phone` so resolution can filter in SQL instead of loading
    #: every pending invite into Python on every portal sign-in.
    normalized_email: Mapped[str | None] = mapped_column(String(255), default=None)
    normalized_phone: Mapped[str | None] = mapped_column(String(32), default=None)

    # CASCADE: the invite exists to seat someone in this directory row: if the
    # row goes, so does the reason to keep chasing a match for it.
    translator_id: Mapped[int | None] = mapped_column(
        ForeignKey("translators.id", ondelete="CASCADE"), default=None
    )
    # CASCADE: same reasoning, for a staff invite.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), default=None
    )
    # SET NULL rather than CASCADE: the invite's own history (who was invited,
    # when, by whom) must survive the suliko.ge admin deactivating an account.
    #
    # Explicit, shortened `name=`: the naming convention's default —
    # "fk_portal_account_invites_portal_translator_id_portal_translators" — is
    # 65 characters, past PostgreSQL's 63-character identifier limit. Must
    # match migration 0007's own explicit name, which is the DDL that actually
    # runs (this table is excluded from 0001's metadata build).
    portal_translator_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "portal_translators.id",
            ondelete="SET NULL",
            name="fk_portal_account_invites_portal_translator_id",
        ),
        default=None,
    )

    status: Mapped[InviteStatus] = mapped_column(
        Enum(
            InviteStatus,
            name="portal_invite_status",
            values_callable=enum_values,
            native_enum=False,
            length=20,
        ),
        default=InviteStatus.PENDING,
        nullable=False,
    )
    invited_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


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
