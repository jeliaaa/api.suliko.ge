"""Translator portal, personal orders, and Google Shared Drive folders.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-15

Eight new tables, in two groups that are handled differently on purpose:

- **Platform tables** (``PLATFORM_TABLES``) — portal translators, their links to
  bureaus, and personal orders. They are read before any tenant is known (to
  find which bureaus a translator works for), or belong to no tenant at all, so
  they get the grant to ``suliko_app`` and NO row-level security. Access is
  decided by the portal identity in ``api/portal_deps.py``. See
  ``models/portal.py`` for why this is the correct trade, and why it is not
  simply a missing policy.

- **Tenant tables** (``TENANT_TABLES``) — a bureau's Shared Drive setting and the
  folders Suliko keeps in it. The same treatment 0002 gave its tables: grant,
  RLS enabled and FORCEd, and the ``tenant_isolation`` policy, driven by the
  same tuple that lists them.

No columns are added to existing tables. Revision 0001 builds its tables from
metadata, so a new column on one of them would also be created by 0001 on a
fresh database, and this revision's ``add_column`` would then fail.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "suliko_app"

#: Dependency order: links and personal orders reference portal_translators.
PLATFORM_TABLES = (
    "portal_translators",
    "portal_translator_links",
    "personal_orders",
    "personal_order_language_pairs",
    "personal_order_files",
)

TENANT_TABLES = (
    "drive_settings",
    "order_drive_folders",
    "order_document_drive_folders",
)

NEW_TABLES = PLATFORM_TABLES + TENANT_TABLES


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


def upgrade() -> None:
    # ── portal_translators ──────────────────────────────────────────────────
    op.create_table(
        "portal_translators",
        _id(),
        sa.Column("external_user_id", sa.String(length=450), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_portal_translators"),
        sa.UniqueConstraint("external_user_id", name="uq_portal_translators_external_user_id"),
    )

    # ── portal_translator_links ─────────────────────────────────────────────
    op.create_table(
        "portal_translator_links",
        _id(),
        sa.Column("portal_translator_id", sa.BigInteger(), nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("translator_id", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["portal_translator_id"],
            ["portal_translators.id"],
            name="fk_portal_translator_links_portal_translator_id_portal_translators",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_portal_translator_links_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["translator_id"],
            ["translators.id"],
            name="fk_portal_translator_links_translator_id_translators",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_portal_translator_links"),
        sa.UniqueConstraint(
            "portal_translator_id", "tenant_id", name="uq_portal_link_translator_tenant"
        ),
        sa.UniqueConstraint("tenant_id", "translator_id", name="uq_portal_link_directory_row"),
    )
    op.create_index("ix_portal_links_tenant", "portal_translator_links", ["tenant_id"])

    # ── personal_orders ─────────────────────────────────────────────────────
    op.create_table(
        "personal_orders",
        _id(),
        sa.Column("portal_translator_id", sa.BigInteger(), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["portal_translator_id"],
            ["portal_translators.id"],
            name="fk_personal_orders_portal_translator_id_portal_translators",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_personal_orders"),
    )
    op.create_index(
        "ix_personal_orders_owner_due", "personal_orders", ["portal_translator_id", "due_date"]
    )

    # ── personal_order_language_pairs ───────────────────────────────────────
    op.create_table(
        "personal_order_language_pairs",
        _id(),
        sa.Column("personal_order_id", sa.BigInteger(), nullable=False),
        sa.Column("source_language", sa.String(length=5), nullable=False),
        sa.Column("target_language", sa.String(length=5), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["personal_order_id"],
            ["personal_orders.id"],
            name="fk_personal_order_language_pairs_personal_order_id_personal_orders",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_personal_order_language_pairs"),
        sa.UniqueConstraint(
            "personal_order_id",
            "source_language",
            "target_language",
            name="uq_personal_order_pair",
        ),
    )

    # ── personal_order_files ────────────────────────────────────────────────
    op.create_table(
        "personal_order_files",
        _id(),
        sa.Column("personal_order_id", sa.BigInteger(), nullable=False),
        # VARCHAR + CHECK rather than a native enum, as in 0002.
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('source', 'translation')", name="ck_personal_order_files_file_kind"
        ),
        sa.ForeignKeyConstraint(
            ["personal_order_id"],
            ["personal_orders.id"],
            name="fk_personal_order_files_personal_order_id_personal_orders",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_personal_order_files"),
    )
    op.create_index(
        "ix_personal_order_files_order_kind",
        "personal_order_files",
        ["personal_order_id", "kind"],
    )

    # ── drive_settings ──────────────────────────────────────────────────────
    op.create_table(
        "drive_settings",
        _id(),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("shared_drive_id", sa.String(length=100), nullable=False),
        sa.Column("drive_name", sa.String(length=255), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_drive_settings_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_drive_settings"),
        sa.UniqueConstraint("tenant_id", name="uq_drive_settings_tenant"),
    )
    op.create_index("ix_drive_settings_tenant_id", "drive_settings", ["tenant_id"])

    # ── order_drive_folders ─────────────────────────────────────────────────
    op.create_table(
        "order_drive_folders",
        _id(),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("folder_id", sa.String(length=100), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_order_drive_folders_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
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

    # ── order_document_drive_folders ────────────────────────────────────────
    op.create_table(
        "order_document_drive_folders",
        _id(),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("order_document_id", sa.BigInteger(), nullable=False),
        sa.Column("folder_id", sa.String(length=100), nullable=False),
        sa.Column("source_folder_id", sa.String(length=100), nullable=False),
        sa.Column("translation_folder_id", sa.String(length=100), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_order_document_drive_folders_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_document_id"],
            ["order_documents.id"],
            name="fk_order_document_drive_folders_order_document_id_order_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_document_drive_folders"),
        sa.UniqueConstraint("order_document_id", name="uq_order_document_drive_folders_document"),
    )
    op.create_index(
        "ix_order_document_drive_folders_tenant_id", "order_document_drive_folders", ["tenant_id"]
    )

    # ── Grants and row-level security ───────────────────────────────────────
    for table in NEW_TABLES:
        op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"))

    for table in TENANT_TABLES:
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

    op.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))

    op.drop_table("order_document_drive_folders")
    op.drop_table("order_drive_folders")
    op.drop_table("drive_settings")
    op.drop_table("personal_order_files")
    op.drop_table("personal_order_language_pairs")
    op.drop_table("personal_orders")
    op.drop_table("portal_translator_links")
    op.drop_table("portal_translators")
