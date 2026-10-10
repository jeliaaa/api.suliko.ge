"""Suliko Translate from an order: one row per machine translation started.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-10

Office can now hand a document's source file to suliko.ge's translator and
file what comes back under the document's translations. ``document_translations``
is what Office remembers about each one: the job, who paid for how many pages,
and the resulting file. See ``models/translation.py``.

## References are emptied, not cascaded

A document or a file that is removed later leaves the row behind with that
column NULL. The pages were spent either way, and the row is the only record
of it on this side.

## RLS

Tenant data, so it gets what 0002, 0004 and 0013 gave theirs: grant, RLS
enabled and FORCEd, and the ``tenant_isolation`` policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "suliko_app"

NEW_TABLES: tuple[str, ...] = ("document_translations",)

TABLE = "document_translations"


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
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("order_document_id", sa.BigInteger(), nullable=True),
        sa.Column("source_file_id", sa.BigInteger(), nullable=True),
        sa.Column("result_file_id", sa.BigInteger(), nullable=True),
        # VARCHAR + CHECK rather than a native enum, as in 0002, 0004 and 0013.
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("suliko_job_id", sa.String(length=100), nullable=False),
        sa.Column("suliko_user_id", sa.String(length=450), nullable=False),
        sa.Column("requested_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("target_language", sa.String(length=5), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
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
            "status IN ('processing', 'completed', 'failed')",
            name="ck_document_translations_translation_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_document_translations_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_document_id"],
            ["order_documents.id"],
            name="fk_document_translations_order_document_id_order_documents",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"],
            ["order_files.id"],
            name="fk_document_translations_source_file_id_order_files",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["result_file_id"],
            ["order_files.id"],
            name="fk_document_translations_result_file_id_order_files",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name="fk_document_translations_requested_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_translations"),
        sa.UniqueConstraint("public_id", name="uq_document_translations_public_id"),
    )
    op.create_index("ix_document_translations_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_document_translations_document", TABLE, ["order_document_id"])
    _isolate(TABLE)
    op.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))


def downgrade() -> None:
    """The record of what was translated goes; the translated files stay."""
    op.drop_table(TABLE)
