"""Months a superuser made free for one account.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-17

``billing_free_months``: one row per account per calendar month that has no fee.
Only the fee is waived; the package's order limit still applies. Tenant data, so it
gets the grant, RLS enabled and FORCEd, and the ``tenant_isolation`` policy, as
0025, 0031 and 0032 gave theirs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "suliko_app"

NEW_TABLES: tuple[str, ...] = ("billing_free_months",)


def upgrade() -> None:
    op.create_table(
        "billing_free_months",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
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
            name="fk_billing_free_months_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_billing_free_months"),
        sa.UniqueConstraint("tenant_id", "period", name="uq_billing_free_months_period"),
    )
    op.create_index("ix_billing_free_months_tenant_id", "billing_free_months", ["tenant_id"])
    op.execute(
        sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON billing_free_months TO {APP_ROLE}")
    )
    op.execute(sa.text("ALTER TABLE billing_free_months ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE billing_free_months FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            CREATE POLICY tenant_isolation ON billing_free_months
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
    """The free months go; every month is billed by its package again."""
    op.drop_table("billing_free_months")
