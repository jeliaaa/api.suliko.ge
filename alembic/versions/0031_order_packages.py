"""Order packages: what a bureau's monthly fee covers, and the invoices for it.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-15

Three things:

- ``billing_packages``: the packages a bureau can be on, the same for everyone
  (three rows, edited by a superuser). Seeded with placeholder numbers:
  package 1 covers 50 orders a month for 50 GEL, package 2 covers 150 for 100 GEL,
  package 3 covers 500 with no listed price (agreed with each bureau), and every
  order beyond the included ones costs 1 GEL. A platform table, so no
  ``tenant_id`` and no row-level security: only the grant to the application role.
- ``billing_invoices``: one row per bureau per calendar month, worked out by a
  superuser and followed by hand (open, sent, paid, waived). Tenant data, so it
  gets the grant, RLS enabled and FORCEd, and the ``tenant_isolation`` policy, as
  0025 gave the import records.
- ``tenants.package_tier``: which package each bureau is on. Every tenant that
  exists starts on 1. One column on a table 0001 owns, hence the guard below
  (see 0006).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "suliko_app"

NEW_TABLES: tuple[str, ...] = ("billing_packages", "billing_invoices")

#: (table, column) added to a table an earlier revision owns.
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (
        "tenants",
        sa.Column(
            "package_tier",
            sa.SmallInteger(),
            server_default="1",
            nullable=False,
        ),
    ),
)

#: Placeholder numbers, to be set by a superuser in the admin console:
#: (tier, orders included, monthly price in tetri or None, one extra order in tetri).
SEED: tuple[tuple[int, int, int | None, int], ...] = (
    (1, 50, 5000, 100),
    (2, 150, 10000, 100),
    (3, 500, None, 100),
)


def _existing_columns(table: str) -> set[str]:
    """What the database already has, or nothing when there is no database
    (the test recorder), so every column looks missing and is recorded."""
    bind = op.get_bind()
    if bind is None:
        return set()
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _timestamps() -> list[sa.Column[object]]:
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
    op.create_table(
        "billing_packages",
        sa.Column("tier", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("orders_included", sa.Integer(), nullable=False),
        sa.Column("monthly_price_cents", sa.Integer(), nullable=True),
        sa.Column("extra_order_cents", sa.Integer(), server_default="100", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("tier", name="pk_billing_packages"),
    )
    for tier, included, price, extra in SEED:
        op.execute(
            sa.text(
                "INSERT INTO billing_packages "
                "(tier, orders_included, monthly_price_cents, extra_order_cents) "
                f"VALUES ({tier}, {included}, {'NULL' if price is None else price}, {extra})"
            )
        )
    op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON billing_packages TO {APP_ROLE}"))

    op.create_table(
        "billing_invoices",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("package_tier", sa.SmallInteger(), nullable=False),
        sa.Column("orders_included", sa.Integer(), nullable=False),
        sa.Column("orders_counted", sa.Integer(), nullable=False),
        sa.Column("extra_orders", sa.Integer(), nullable=False),
        sa.Column("plan_fee_cents", sa.Integer(), nullable=True),
        sa.Column("extra_fee_cents", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_billing_invoices_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_billing_invoices"),
        sa.UniqueConstraint("tenant_id", "period", name="uq_billing_invoices_period"),
    )
    op.create_index("ix_billing_invoices_tenant_id", "billing_invoices", ["tenant_id"])
    op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON billing_invoices TO {APP_ROLE}"))
    op.execute(sa.text("ALTER TABLE billing_invoices ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE billing_invoices FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            CREATE POLICY tenant_isolation ON billing_invoices
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

    for table, column in NEW_COLUMNS:
        if column.name not in _existing_columns(table):
            op.add_column(table, column)


def downgrade() -> None:
    """The packages, the invoices and each bureau's package go.

    Nothing else depends on them: orders are never held back by a package.
    """
    for table, column in reversed(NEW_COLUMNS):
        existing = _existing_columns(table)
        if not existing or column.name in existing:
            op.drop_column(table, str(column.name))
    op.drop_table("billing_invoices")
    op.drop_table("billing_packages")
