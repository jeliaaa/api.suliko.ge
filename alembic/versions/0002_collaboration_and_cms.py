"""Order comments, the notification feed, and the marketing-site CMS.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-15

Six new tables, all tenant-scoped, so all six need the same three things the
bootstrap revision gave the original tables: the grant to ``suliko_app``, RLS
enabled and FORCEd, and the ``tenant_isolation`` policy. A table that gets the
grant but not the policy is readable across tenants — that is the failure mode
this revision is shaped to avoid, which is why the RLS loop is driven by the
same list that creates the tables rather than a second hand-written one.

Written as explicit operations, not ``create_all``: 0001 documents why it is
the only revision allowed to build from metadata.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "suliko_app"

#: Created in dependency order; dropped in reverse. `notifications` references
#: `order_comments`, so the order matters in both directions.
NEW_TABLES = (
    "order_comments",
    "order_comment_mentions",
    "order_comment_reads",
    "notifications",
    "service_pages",
    "site_strings",
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


def upgrade() -> None:
    # ── order_comments ──────────────────────────────────────────────────────
    op.create_table(
        "order_comments",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("author_user_id", sa.BigInteger(), nullable=True),
        sa.Column("author_name", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_order_comments_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_comments_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name="fk_order_comments_author_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_comments"),
    )
    op.create_index("ix_order_comments_tenant_id", "order_comments", ["tenant_id"])
    op.create_index(
        "ix_order_comments_tenant_order",
        "order_comments",
        ["tenant_id", "order_id", "created_at"],
    )
    op.create_index(
        "ix_order_comments_tenant_author", "order_comments", ["tenant_id", "author_user_id"]
    )

    # ── order_comment_mentions ──────────────────────────────────────────────
    op.create_table(
        "order_comment_mentions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("comment_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_order_comment_mentions_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["order_comments.id"],
            name="fk_order_comment_mentions_comment_id_order_comments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_comment_mentions_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_order_comment_mentions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_comment_mentions"),
        sa.UniqueConstraint("comment_id", "user_id", name="uq_mention_once_per_comment"),
    )
    op.create_index("ix_order_comment_mentions_tenant_id", "order_comment_mentions", ["tenant_id"])
    op.create_index("ix_mentions_tenant_user", "order_comment_mentions", ["tenant_id", "user_id"])

    # ── order_comment_reads ─────────────────────────────────────────────────
    # No created_at/updated_at: the watermark IS the timestamp.
    op.create_table(
        "order_comment_reads",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "read_through",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_order_comment_reads_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_order_comment_reads_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_comment_reads_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_comment_reads"),
        sa.UniqueConstraint("user_id", "order_id", name="uq_read_once_per_order"),
    )
    op.create_index("ix_order_comment_reads_tenant_id", "order_comment_reads", ["tenant_id"])
    op.create_index("ix_comment_reads_tenant_user", "order_comment_reads", ["tenant_id", "user_id"])

    # ── notifications ───────────────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        # VARCHAR + CHECK rather than a native enum: adding a kind is then an
        # ALTER of one constraint, not a type migration that locks the table.
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=False),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_name", sa.String(length=255), nullable=True),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("comment_id", sa.BigInteger(), nullable=True),
        sa.Column("subject_label", sa.String(length=255), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('comment', 'mention', 'status_change', 'payment', 'order_created', 'system')",
            name="ck_notifications_notification_kind",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_notifications_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_notifications_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name="fk_notifications_actor_user_id_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_notifications_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["order_comments.id"],
            name="fk_notifications_comment_id_order_comments",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
    )
    op.create_index("ix_notifications_tenant_id", "notifications", ["tenant_id"])
    op.create_index(
        "ix_notifications_tenant_user", "notifications", ["tenant_id", "user_id", "created_at"]
    )
    op.create_index(
        "ix_notifications_tenant_unread", "notifications", ["tenant_id", "user_id", "read_at"]
    )

    # ── service_pages ───────────────────────────────────────────────────────
    op.create_table(
        "service_pages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("locale", sa.String(length=5), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("meta_title", sa.String(length=255), nullable=True),
        sa.Column("meta_description", sa.String(length=500), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'draft'")
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by_user_id", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("status IN ('draft', 'published')", name="ck_service_pages_page_status"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_service_pages_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_service_pages_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_service_pages"),
        sa.UniqueConstraint("tenant_id", "slug", "locale", name="uq_service_page_slug_locale"),
    )
    op.create_index("ix_service_pages_tenant_id", "service_pages", ["tenant_id"])
    op.create_index("ix_service_pages_tenant_status", "service_pages", ["tenant_id", "status"])

    # ── site_strings ────────────────────────────────────────────────────────
    op.create_table(
        "site_strings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("locale", sa.String(length=5), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "group_name", sa.String(length=60), nullable=False, server_default=sa.text("'general'")
        ),
        sa.Column("context_note", sa.String(length=255), nullable=True),
        sa.Column("updated_by_user_id", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_site_strings_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_site_strings_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_site_strings"),
        sa.UniqueConstraint("tenant_id", "key", "locale", name="uq_site_string_key_locale"),
    )
    op.create_index("ix_site_strings_tenant_id", "site_strings", ["tenant_id"])
    op.create_index("ix_site_strings_tenant_group", "site_strings", ["tenant_id", "group_name"])

    # ── Grants and row-level security ───────────────────────────────────────
    # The 0001 grant was `ON ALL TABLES`, which is a snapshot — it does not
    # cover tables created afterwards. Every new table needs its own grant.
    for table in NEW_TABLES:
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

    op.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))


def downgrade() -> None:
    for table in reversed(NEW_TABLES):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))

    op.drop_table("site_strings")
    op.drop_table("service_pages")
    op.drop_table("notifications")
    op.drop_table("order_comment_reads")
    op.drop_table("order_comment_mentions")
    op.drop_table("order_comments")
