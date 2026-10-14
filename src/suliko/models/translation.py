"""Machine translations of an order's documents, done by Suliko Translate.

One row per translation someone started: bureau staff from Suliko Office, or the
translator a document is assigned to, from suliko.ge's Orders tab. The work
happens on suliko.ge (``integrations/suliko_translate.py``), which charges the
pages to the person who pressed the button; this row is what Office remembers
about it:
which source file went in, the job to ask suliko.ge about, how many pages it
cost and whom, and, once it is done, the translation file that came back.

The row outlives its parts on purpose. A document or a file that is later
removed leaves the row behind with that reference emptied, so "who spent pages
on what" can still be answered.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin, enum_values


class TranslationStatus(enum.StrEnum):
    """Where a translation stands. Only ``processing`` is asked about again."""

    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DocumentTranslation(Base, IdMixin, TenantScoped, TimestampMixin):
    __tablename__ = "document_translations"
    __table_args__ = (Index("ix_document_translations_document", "order_document_id"),)

    #: What URLs and the frontend carry, as with files. The integer id stays here.
    public_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    order_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_documents.id", ondelete="SET NULL"), default=None
    )
    #: The file that was translated, and the translation that came back.
    source_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_files.id", ondelete="SET NULL"), default=None
    )
    result_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_files.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[TranslationStatus] = mapped_column(
        Enum(
            TranslationStatus,
            name="translation_status",
            values_callable=enum_values,
            native_enum=False,
            length=20,
        ),
        nullable=False,
    )
    #: suliko.ge's own id for the job: what its status and result are asked by.
    suliko_job_id: Mapped[str] = mapped_column(String(100), nullable=False)
    #: Whose balance paid: the suliko.ge account of the person who started it.
    suliko_user_id: Mapped[str] = mapped_column(String(450), nullable=False)
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    #: Set instead when a translator started it from suliko.ge's Orders tab:
    #: their suliko.ge account. NULL for one started in Office. The result is
    #: then filed as theirs, so they can remove it like any file they uploaded.
    requested_by_portal_user_id: Mapped[str | None] = mapped_column(String(450), default=None)
    #: The Office language code translated into, as the document had it then.
    target_language: Mapped[str] = mapped_column(String(5), nullable=False)
    #: The pages suliko.ge measured, which is what it charged.
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The translation as HTML: what the page that corrects it edits, and what
    #: the Word file in ``result_file_id`` is built from again on every save.
    #: NULL for one made before the file became a Word document, whose file
    #: is the HTML, and for one the HTML of which was too large to keep.
    html: Mapped[str | None] = mapped_column(Text, default=None)
    #: Why it failed, in a few words, for the person looking at the order.
    error: Mapped[str | None] = mapped_column(String(500), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
