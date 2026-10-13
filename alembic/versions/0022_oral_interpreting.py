"""Oral interpreting lines: a service type on documents, a per-minute rate.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-13

- ``order_documents.service_type``: 'written' (every existing line) or 'oral'.
- ``order_documents.document_type_id`` becomes nullable: an oral line has no
  document.
- ``language_pair_prices.price_per_minute``: nullable, what an oral line is
  priced from.

Same guard as 0018 to 0020: 0001 builds from the current models, so on a fresh
database the columns exist already when this runs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Columns added, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (
        "order_documents",
        sa.Column(
            "service_type",
            sa.String(length=10),
            server_default="written",
            nullable=False,
        ),
    ),
    ("language_pair_prices", sa.Column("price_per_minute", sa.Numeric(10, 2), nullable=True)),
)


def _existing_columns(table: str) -> set[str]:
    bind = op.get_bind()
    if bind is None:
        return set()
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    for table, column in NEW_COLUMNS:
        if column.name not in _existing_columns(table):
            op.add_column(table, column)
    op.alter_column("order_documents", "document_type_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    """Oral lines have no document type, so they cannot survive the old NOT NULL:
    they go (an order keeps at least its written lines)."""
    op.execute(sa.text("DELETE FROM order_documents WHERE service_type = 'oral'"))
    op.alter_column(
        "order_documents", "document_type_id", existing_type=sa.BigInteger(), nullable=False
    )
    for table, column in reversed(NEW_COLUMNS):
        existing = _existing_columns(table)
        if not existing or column.name in existing:
            op.drop_column(table, str(column.name))
