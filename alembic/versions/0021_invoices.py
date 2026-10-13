"""Invoice numbers: one row per order, written the first time it is invoiced.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-13

Until now an invoice's number was the order's number in the bureau (1, 2, 3...),
worked out on every view. ``invoices`` keeps a number allocated once (year and
month plus a random suffix) with the day it was issued, so a reprint is the
same document and the number does not show how many invoices a bureau has made.
Orders that were already invoiced simply get a row the next time they are opened.

Tenant data, so it gets what 0017 gave its table: grant, RLS enabled and
FORCEd, and the ``tenant_isolation`` policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "suliko_app"

NEW_TABLES: tuple[str, ...] = ("invoices",)

TABLE = "invoices"


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
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("number", sa.String(length=20), nullable=False),
        sa.Column("issued_on", sa.Date(), nullable=False),
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
            name="fk_invoices_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_invoices_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_invoices"),
        sa.UniqueConstraint("tenant_id", "order_id", name="uq_invoices_tenant_order"),
        sa.UniqueConstraint("tenant_id", "number", name="uq_invoices_tenant_number"),
    )
    op.create_index("ix_invoices_tenant_id", TABLE, ["tenant_id"])
    _isolate(TABLE)
    op.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))


def downgrade() -> None:
    """The numbers go; invoices are computed, so nothing else is lost."""
    op.drop_table(TABLE)
