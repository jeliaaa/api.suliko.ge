"""Packages per audience, a hard limit instead of a per-order fee.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-17

The rule changed on 2026-10-17: when an account's orders for the month reach its
package, new orders are refused until it moves up a package, and nothing is charged
per order. So:

- ``billing_packages`` loses ``extra_order_cents`` and gains ``audience``
  ("bureau" or "freelancer"); the key becomes (audience, tier). Existing rows are
  the bureau's. Three freelancer rows are added, with placeholder numbers.
- ``billing_invoices`` loses ``extra_orders`` and ``extra_fee_cents``. An invoice
  already written keeps its ``total_cents`` as it was asked.

Both tables belong to 0031, so no fresh database builds them ahead of this.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Placeholders for freelancers: (tier, orders included, monthly fee in tetri or None).
FREELANCER_SEED: tuple[tuple[int, int, int | None], ...] = (
    (1, 50, 0),
    (2, 150, 2000),
    (3, 500, None),
)


def upgrade() -> None:
    op.add_column(
        "billing_packages",
        sa.Column("audience", sa.String(length=10), server_default="bureau", nullable=False),
    )
    op.drop_constraint("pk_billing_packages", "billing_packages", type_="primary")
    op.create_primary_key("pk_billing_packages", "billing_packages", ["audience", "tier"])
    op.drop_column("billing_packages", "extra_order_cents")
    for tier, included, price in FREELANCER_SEED:
        op.execute(
            sa.text(
                "INSERT INTO billing_packages (audience, tier, orders_included, monthly_price_cents) "
                f"VALUES ('freelancer', {tier}, {included}, {'NULL' if price is None else price})"
            )
        )
    op.drop_column("billing_invoices", "extra_orders")
    op.drop_column("billing_invoices", "extra_fee_cents")


def downgrade() -> None:
    """Back to one set of packages and a per-order fee column (set to nothing)."""
    op.add_column(
        "billing_invoices",
        sa.Column("extra_fee_cents", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "billing_invoices",
        sa.Column("extra_orders", sa.Integer(), server_default="0", nullable=False),
    )
    op.execute(sa.text("DELETE FROM billing_packages WHERE audience <> 'bureau'"))
    op.add_column(
        "billing_packages",
        sa.Column("extra_order_cents", sa.Integer(), server_default="100", nullable=False),
    )
    op.drop_constraint("pk_billing_packages", "billing_packages", type_="primary")
    op.create_primary_key("pk_billing_packages", "billing_packages", ["tier"])
    op.drop_column("billing_packages", "audience")
