"""Claude Translate: a queue of files for a separate Claude Code server.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-30

* ``claude_translation_jobs``: one row per file a person asked Claude to
  translate; the worker claims them, translates and files the Word document
  back. See ``models/claude_translation.py``.
* ``claude_worker_heartbeats``: when a worker last asked for work. A platform
  table (no ``tenant_id``, no row-level security), like ``accounts``.
* ``tenants.claude_translate_enabled``: the per-organisation switch, off for
  every tenant that exists.
* ``orders.translation_instructions``: text sent to the worker with each file.

## RLS

The jobs are tenant data, so they get what 0017 gave ``document_translations``:
grant, RLS enabled and FORCEd, and the ``tenant_isolation`` policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "suliko_app"

NEW_TABLES: tuple[str, ...] = ("claude_translation_jobs", "claude_worker_heartbeats")

JOBS = "claude_translation_jobs"
HEARTBEATS = "claude_worker_heartbeats"

#: Columns added to tables an earlier revision owns, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (
        "tenants",
        sa.Column(
            "claude_translate_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    ),
    ("orders", sa.Column("translation_instructions", sa.Text(), nullable=True)),
)


def _existing_columns(table: str) -> set[str]:
    """What the database already has, or nothing when there is no database
    (the test recorder), so the column looks missing and is recorded."""
    bind = op.get_bind()
    if bind is None:
        return set()
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _isolate(table: str) -> None:
    op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"))
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (
                tenant_id = NULLIF(current_setting('suliko.tenant_id', true), '')::bigint
            )
            WITH CHECK (
                tenant_id = NULLIF(current_setting('suliko.tenant_id', true), '')::bigint
            );
            """
        )
    )


def upgrade() -> None:
    for table, column in NEW_COLUMNS:
        if column.name not in _existing_columns(table):
            op.add_column(table, column)

    op.create_table(
        JOBS,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("order_document_id", sa.BigInteger(), nullable=True),
        sa.Column("source_file_id", sa.BigInteger(), nullable=True),
        sa.Column("result_file_id", sa.BigInteger(), nullable=True),
        # VARCHAR + CHECK rather than a native enum, as in 0002, 0004 and 0013.
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("source_language", sa.String(length=5), nullable=False),
        sa.Column("target_language", sa.String(length=5), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("requested_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'translating', 'translated', 'failed', 'cancelled')",
            name="ck_claude_translation_jobs_claude_job_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_claude_translation_jobs_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_claude_translation_jobs_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_document_id"],
            ["order_documents.id"],
            name="fk_claude_translation_jobs_order_document_id_order_documents",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"],
            ["order_files.id"],
            name="fk_claude_translation_jobs_source_file_id_order_files",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["result_file_id"],
            ["order_files.id"],
            name="fk_claude_translation_jobs_result_file_id_order_files",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name="fk_claude_translation_jobs_requested_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_claude_translation_jobs"),
        sa.UniqueConstraint("public_id", name="uq_claude_translation_jobs_public_id"),
    )
    op.create_index("ix_claude_translation_jobs_tenant_id", JOBS, ["tenant_id"])
    op.create_index(
        "ix_claude_translation_jobs_tenant_status", JOBS, ["tenant_id", "status", "created_at"]
    )
    op.create_index("ix_claude_translation_jobs_order", JOBS, ["order_id"])
    op.create_index(
        "uq_claude_translation_jobs_active_file",
        JOBS,
        ["source_file_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'translating')"),
    )
    _isolate(JOBS)

    op.create_table(
        HEARTBEATS,
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("name", name="pk_claude_worker_heartbeats"),
    )
    op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {HEARTBEATS} TO {APP_ROLE}"))
    op.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))


def downgrade() -> None:
    """The queue goes, with the switch and the instructions. Word documents the
    worker already filed stay: they are order files."""
    op.drop_table(HEARTBEATS)
    op.drop_table(JOBS)
    for table, column in reversed(NEW_COLUMNS):
        existing = _existing_columns(table)
        if not existing or column.name in existing:
            op.drop_column(table, str(column.name))
