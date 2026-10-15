"""A bureau's own invoice: one Word file with placeholders.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-15

``invoice_templates``: at most one per bureau, the uploaded .docx kept as bytes.
Tenant data, so it gets the grant, RLS enabled and FORCEd, and the
``tenant_isolation`` policy, as 0025 and 0031 gave theirs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "suliko_app"

NEW_TABLES: tuple[str, ...] = ("invoice_templates",)


def upgrade() -> None:
    op.create_table(
        "invoice_templates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
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
            name="fk_invoice_templates_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invoice_templates"),
        sa.UniqueConstraint("tenant_id", name="uq_invoice_templates_tenant"),
    )
    op.create_index("ix_invoice_templates_tenant_id", "invoice_templates", ["tenant_id"])
    op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON invoice_templates TO {APP_ROLE}"))
    op.execute(sa.text("ALTER TABLE invoice_templates ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE invoice_templates FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            CREATE POLICY tenant_isolation ON invoice_templates
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
    """The uploaded files go; invoices use the standard layout again."""
    op.drop_table("invoice_templates")
