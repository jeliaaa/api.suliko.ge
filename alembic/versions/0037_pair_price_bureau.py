"""A freelancer's rate for orders that come from a bureau.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-22

``language_pair_prices.price_per_page_bureau``: what a bureau pays the
freelancer per page for a language pair, kept apart from ``price_per_page``
(what the freelancer charges their own clients). NULL means not set, which is
every row made before the column existed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

TABLE = "language_pair_prices"
COLUMN = sa.Column("price_per_page_bureau", sa.Numeric(10, 2), nullable=True)


def _existing_columns() -> set[str]:
    """What the database already has, or nothing when there is no database
    (the test recorder), so the column looks missing and is recorded."""
    bind = op.get_bind()
    if bind is None:
        return set()
    return {column["name"] for column in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    if COLUMN.name not in _existing_columns():
        op.add_column(TABLE, COLUMN)


def downgrade() -> None:
    existing = _existing_columns()
    if not existing or COLUMN.name in existing:
        op.drop_column(TABLE, str(COLUMN.name))
