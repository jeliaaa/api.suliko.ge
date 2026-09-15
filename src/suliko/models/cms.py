"""Content for the public marketing site.

Two things, deliberately kept apart:

- `ServicePage` — a landing page per service, per locale. Long-form content
  someone writes and edits.
- `SiteString` — one i18n key for the marketing site's chrome (nav labels,
  button text). Short, and there are hundreds of them.

They are separate tables because they are edited by different people in
different ways: a page is a document with a title and a body, a string is a
cell in a spreadsheet-like grid. Folding them together would give both screens
the wrong shape.

## Locale, not tenant language

Content is keyed by `(slug, locale)` and `(key, locale)`. A tenant may publish
the Georgian page and leave the English one unwritten, so the API returns
whatever exists rather than assuming both.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin, enum_values


class PageStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class ServicePage(Base, IdMixin, TenantScoped, TimestampMixin):
    """One service landing page, in one locale."""

    __tablename__ = "service_pages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", "locale", name="uq_service_page_slug_locale"),
        Index("ix_service_pages_tenant_status", "tenant_id", "status"),
    )

    #: URL segment, e.g. `notarised-translation`. Lowercase, hyphenated.
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    locale: Mapped[str] = mapped_column(String(5), nullable=False)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    #: One-line summary for cards and search results.
    summary: Mapped[str | None] = mapped_column(String(500), default=None)
    #: Markdown. Rendered by the public site, never by this API — rendering
    #: here would mean deciding a sanitisation policy in the wrong place.
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)

    meta_title: Mapped[str | None] = mapped_column(String(255), default=None)
    meta_description: Mapped[str | None] = mapped_column(String(500), default=None)

    status: Mapped[PageStatus] = mapped_column(
        Enum(
            PageStatus,
            name="page_status",
            values_callable=enum_values,
            native_enum=False,
            length=20,
        ),
        default=PageStatus.DRAFT,
        nullable=False,
    )
    #: Ordering within the services list on the public site.
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class SiteString(Base, IdMixin, TenantScoped, TimestampMixin):
    """One translatable string for the public site's chrome."""

    __tablename__ = "site_strings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", "locale", name="uq_site_string_key_locale"),
        Index("ix_site_strings_tenant_group", "tenant_id", "group_name"),
    )

    #: Dotted key, e.g. `nav.services`. Matches the public site's message files.
    key: Mapped[str] = mapped_column(String(160), nullable=False)
    locale: Mapped[str] = mapped_column(String(5), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    #: First segment of the key, stored so the editor can group without
    #: parsing every key on every request.
    group_name: Mapped[str] = mapped_column(String(60), default="general", nullable=False)
    #: Note to whoever translates it — where it appears, length limits.
    context_note: Mapped[str | None] = mapped_column(String(255), default=None)

    updated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
