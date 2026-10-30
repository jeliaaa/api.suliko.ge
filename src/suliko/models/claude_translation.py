"""Claude Translate: a person picks files, a separate Claude Code server translates them.

The click only queues work. One row per file per request waits here until the
worker, a Claude Code server run by the platform, asks for it
(``api/v1/claude_worker.py``), downloads the file, translates it and sends a
Word document back, which is filed under the document's Translation files like
any other. Nothing runs when a file is merely uploaded, and a file nobody
picked is never touched.

The queue is tenant data (RLS like every table that has ``tenant_id``); the
worker serves all organisations that have the feature, one tenant scope at a
time. Its heartbeat is not tenant data and has no ``tenant_id``.

Rows outlive their parts, as ``document_translations`` do: a removed document or
file leaves the job behind with that reference emptied.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin, enum_values


class ClaudeJobStatus(enum.StrEnum):
    """Where a job stands.

    ``translated`` means a draft for the translator, not a finished certified
    document.
    """

    QUEUED = "queued"
    TRANSLATING = "translating"
    TRANSLATED = "translated"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: A job in one of these is waiting for, or with, the worker.
ACTIVE_STATUSES = (ClaudeJobStatus.QUEUED, ClaudeJobStatus.TRANSLATING)


class ClaudeTranslationJob(Base, IdMixin, TenantScoped, TimestampMixin):
    __tablename__ = "claude_translation_jobs"
    __table_args__ = (
        Index("ix_claude_translation_jobs_tenant_status", "tenant_id", "status", "created_at"),
        Index("ix_claude_translation_jobs_order", "order_id"),
        # At most one job at a time per file: a second click while the first
        # is waiting or running is refused by the database, not just by a check
        # two requests could pass together.
        Index(
            "uq_claude_translation_jobs_active_file",
            "source_file_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'translating')"),
            sqlite_where=text("status IN ('queued', 'translating')"),
        ),
    )

    #: What the frontend carries. The worker is given it with the tenant in front
    #: (``<tenant id>.<public id>``) because it serves every tenant.
    public_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    order_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_documents.id", ondelete="SET NULL"), default=None
    )
    #: The file to translate, and the Word document that came back.
    source_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_files.id", ondelete="SET NULL"), default=None
    )
    result_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_files.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[ClaudeJobStatus] = mapped_column(
        Enum(
            ClaudeJobStatus,
            name="claude_job_status",
            values_callable=enum_values,
            native_enum=False,
            length=20,
        ),
        default=ClaudeJobStatus.QUEUED,
        nullable=False,
    )
    #: What the worker is told, as it stood when the person clicked: a later
    #: edit of the document or the order does not change a job already queued.
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    source_language: Mapped[str] = mapped_column(String(5), nullable=False)
    target_language: Mapped[str] = mapped_column(String(5), nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, default=None)

    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    #: Why it failed or was put back, in a few words.
    error: Mapped[str | None] = mapped_column(String(500), default=None)
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ClaudeWorkerHeartbeat(Base):
    """When a worker last asked for work: how support sees that it is down.

    Platform data, not a tenant's: no ``tenant_id``, no row-level security.
    """

    __tablename__ = "claude_worker_heartbeats"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Free text the worker sends, such as its version. Never shown to tenants.
    detail: Mapped[str | None] = mapped_column(String(500), default=None)
