"""Order files move from Google Shared Drives to Suliko's own storage.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-06

Shared Drives exist only on paid Google Workspace accounts, and most bureaus
do not have one — so most bureaus could not attach a single file to an order.
Suliko now stores the files itself (``integrations/object_storage.py``) and
keeps what a person sees about each one here, in ``order_files``.

## What goes

The three Drive tables: which Shared Drive a bureau linked, and the folder ids
Suliko recorded inside it. Nothing in them is a file — the files themselves
were in Google, and stay there; a bureau that had a drive linked still has
every file in it. What is lost is only Suliko's pointer to those folders.

## RLS

``order_files`` is tenant data and gets the treatment 0002 and 0004 gave
theirs: grant, RLS enabled and FORCEd, and the ``tenant_isolation`` policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "suliko_app"

NEW_TABLES: tuple[str, ...] = ("order_files",)

#: Created by 0004, gone from the models. Listed so the parity tests can tell
#: "dropped on purpose" from "migrated but not modelled".
DROPPED_TABLES: tuple[str, ...] = (
    "order_document_drive_folders",
    "order_drive_folders",
    "drive_settings",
)


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
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
    ]


def _id() -> sa.Column[sa.BigInteger]:
    return sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False)


def _tenant_id(table: str) -> list[sa.SchemaItem]:
    return [
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=f"fk_{table}_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
    ]


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
        "order_files",
        _id(),
        *_tenant_id("order_files"),
        sa.Column("public_id", sa.String(length=32), nullable=False),
        sa.Column("order_document_id", sa.BigInteger(), nullable=True),
        # VARCHAR + CHECK rather than a native enum, as in 0002 and 0004.
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("uploaded_by", sa.String(length=500), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(length=500), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("kind IN ('source', 'translation')", name="ck_order_files_file_kind"),
        sa.ForeignKeyConstraint(
            ["order_document_id"],
            ["order_documents.id"],
            name="fk_order_files_order_document_id_order_documents",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_files"),
        sa.UniqueConstraint("public_id", name="uq_order_files_public_id"),
    )
    op.create_index("ix_order_files_tenant_id", "order_files", ["tenant_id"])
    op.create_index("ix_order_files_document_kind", "order_files", ["order_document_id", "kind"])
    _isolate("order_files")
    op.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))

    # Policies go with their tables.
    for table in DROPPED_TABLES:
        op.drop_table(table)


def downgrade() -> None:
    """Back to the Drive tables — empty. Links have to be made again."""
    op.drop_table("order_files")

    op.create_table(
        "drive_settings",
        _id(),
        *_tenant_id("drive_settings"),
        sa.Column("shared_drive_id", sa.String(length=100), nullable=False),
        sa.Column("drive_name", sa.String(length=255), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_drive_settings"),
        sa.UniqueConstraint("tenant_id", name="uq_drive_settings_tenant"),
    )
    op.create_index("ix_drive_settings_tenant_id", "drive_settings", ["tenant_id"])

    op.create_table(
        "order_drive_folders",
        _id(),
        *_tenant_id("order_drive_folders"),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("folder_id", sa.String(length=100), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_drive_folders_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_drive_folders"),
        sa.UniqueConstraint("order_id", name="uq_order_drive_folders_order"),
    )
    op.create_index("ix_order_drive_folders_tenant_id", "order_drive_folders", ["tenant_id"])

    op.create_table(
        "order_document_drive_folders",
        _id(),
        *_tenant_id("order_document_drive_folders"),
        sa.Column("order_document_id", sa.BigInteger(), nullable=False),
        sa.Column("folder_id", sa.String(length=100), nullable=False),
        sa.Column("source_folder_id", sa.String(length=100), nullable=False),
        sa.Column("translation_folder_id", sa.String(length=100), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["order_document_id"],
            ["order_documents.id"],
            name="fk_doc_drive_folder_document",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_document_drive_folders"),
        sa.UniqueConstraint("order_document_id", name="uq_order_document_drive_folders_document"),
    )
    op.create_index(
        "ix_order_document_drive_folders_tenant_id", "order_document_drive_folders", ["tenant_id"]
    )

    for table in DROPPED_TABLES:
        _isolate(table)
