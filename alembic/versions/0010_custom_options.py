"""Bureau-defined dropdown values: acquisition sources and order statuses.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-30

One tenant-scoped table, ``custom_options``, with the same grant and
row-level-security policy every tenant table gets (see 0006). No columns are
added to existing tables, so none of 0006's column guard is needed here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None

APP_ROLE = "suliko_app"

NEW_TABLES: tuple[str, ...] = ("custom_options",)


def upgrade() -> None:
    op.create_table(
        "custom_options",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("list_key", sa.String(length=40), nullable=False),
        sa.Column("value", sa.String(length=60), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_custom_options_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_custom_options"),
        sa.UniqueConstraint(
            "tenant_id", "list_key", "value", name="uq_custom_options_tenant_list_value"
        ),
    )
    op.create_index("ix_custom_options_tenant_id", "custom_options", ["tenant_id"])
    op.create_index("ix_custom_options_tenant_list", "custom_options", ["tenant_id", "list_key"])

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
    op.drop_table("custom_options")
