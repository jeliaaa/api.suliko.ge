"""Order comments, mentions and read state — what drives the bell.

The PHP calls these `translation_comments`, `translation_comment_mentions` and
`translation_comment_reads`. The shape is kept; only the naming follows this
codebase's `order_*` convention.

## Why a read watermark and not an `is_read` flag

A comment is seen by many people. `is_read` on the comment row would be one
boolean shared between everyone, so the first person to open the order would
mark it read for the whole office. `OrderCommentRead` is one row per (user,
comment) instead, which is what makes "Unread (3)" mean three unread *by you*.

## Why mentions are a table and not a text scan

The bell has to answer "who was mentioned" without re-parsing every comment
body, and a username can change. Resolving `@name` to a user id once, at write
time, means a later rename does not silently break the mention.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin, enum_values


class NotificationKind(enum.StrEnum):
    """What produced the entry, driving the icon and the feed tabs."""

    COMMENT = "comment"
    MENTION = "mention"
    STATUS_CHANGE = "status_change"
    PAYMENT = "payment"
    ORDER_CREATED = "order_created"
    SYSTEM = "system"


class OrderComment(Base, IdMixin, TenantScoped, TimestampMixin):
    """An internal staff note on an order.

    Internal: never shown to a client. The staff-to-client thread is a
    different table in the PHP (`translation_messages`) and stays separate
    here for the same reason — one accidental join must not leak a private
    note into a client-facing view.
    """

    __tablename__ = "order_comments"
    __table_args__ = (
        Index("ix_order_comments_tenant_order", "tenant_id", "order_id", "created_at"),
        Index("ix_order_comments_tenant_author", "tenant_id", "author_user_id"),
    )

    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    author_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    #: Denormalised so a deleted user's comments still say who wrote them.
    #: The FK above goes null; this does not.
    author_name: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Pinned comments sort to the top of the order's thread.
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Set instead of deleting, so the thread keeps its shape and the audit
    #: trail stays intact.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class OrderCommentMention(Base, IdMixin, TenantScoped, TimestampMixin):
    """One `@user` inside one comment."""

    __tablename__ = "order_comment_mentions"
    __table_args__ = (
        UniqueConstraint("comment_id", "user_id", name="uq_mention_once_per_comment"),
        Index("ix_mentions_tenant_user", "tenant_id", "user_id"),
    )

    comment_id: Mapped[int] = mapped_column(
        ForeignKey("order_comments.id", ondelete="CASCADE"), nullable=False
    )
    #: Denormalised from the comment so the feed can filter by order without
    #: a join back through comments.
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)


class Notification(Base, IdMixin, TenantScoped, TimestampMixin):
    """One entry in one user's feed.

    Fanned out at write time — one row per recipient — rather than computed
    per request from comments, payments and status events. Two reasons: the
    feed is read far more often than it is written, and "read" is per user,
    which a computed feed has nowhere to store.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_tenant_user", "tenant_id", "user_id", "created_at"),
        Index("ix_notifications_tenant_unread", "tenant_id", "user_id", "read_at"),
    )

    #: Who sees it.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[NotificationKind] = mapped_column(
        Enum(
            NotificationKind,
            name="notification_kind",
            values_callable=enum_values,
            native_enum=False,
            length=30,
        ),
        nullable=False,
    )
    #: Rendered text. Stored rather than templated at read time so the entry
    #: still reads correctly after the thing it describes has changed.
    body: Mapped[str] = mapped_column(String(500), nullable=False)

    #: Who caused it. Null for automated entries — that is what the
    #: "Automated" tab filters on.
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    actor_name: Mapped[str | None] = mapped_column(String(255), default=None)

    #: What it is about. Both nullable so a system notice needs no target.
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), default=None
    )
    comment_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_comments.id", ondelete="CASCADE"), default=None
    )
    #: Denormalised for the feed's "{client} #{order}" line.
    subject_label: Mapped[str | None] = mapped_column(String(255), default=None)

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class OrderCommentRead(Base, IdMixin, TenantScoped):
    """Per-user read watermark on an order's comment thread.

    Separate from `Notification.read_at`: opening an order marks the whole
    thread read, which is not the same event as dismissing one bell entry.
    """

    __tablename__ = "order_comment_reads"
    __table_args__ = (
        UniqueConstraint("user_id", "order_id", name="uq_read_once_per_order"),
        Index("ix_comment_reads_tenant_user", "tenant_id", "user_id"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    #: Everything created at or before this instant is read.
    read_through: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
