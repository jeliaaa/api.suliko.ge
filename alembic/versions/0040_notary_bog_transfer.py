"""Notary payments started through Bank of Georgia Business Online.

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-12

A transfer is created in the bank unsigned and the owner signs it in the
bank's app, so the payment keeps the bank's ids and where it stands. Existing
payments are untouched (all five columns are NULL for them).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notary_payments", sa.Column("bog_unique_id", sa.String(36), nullable=True))
    op.add_column("notary_payments", sa.Column("bog_unique_key", sa.BigInteger(), nullable=True))
    op.add_column("notary_payments", sa.Column("bog_status", sa.String(60), nullable=True))
    op.add_column("notary_payments", sa.Column("bog_state", sa.String(20), nullable=True))
    op.add_column(
        "notary_payments", sa.Column("bog_checked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_notary_payments_bog_state",
        "notary_payments",
        ["tenant_id", "bog_state"],
        postgresql_where=sa.text("bog_state IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_notary_payments_bog_state", table_name="notary_payments")
    op.drop_column("notary_payments", "bog_checked_at")
    op.drop_column("notary_payments", "bog_state")
    op.drop_column("notary_payments", "bog_status")
    op.drop_column("notary_payments", "bog_unique_key")
    op.drop_column("notary_payments", "bog_unique_id")
