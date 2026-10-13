"""Where a translator certifies: a list of city codes on ``translators``.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-13

One nullable column, ``cities``: codes from ``domain.cities`` joined by commas
with a comma at each end, so a filter is a LIKE on ",code,". Null for every
existing translator. Same guard as 0018 and 0019: 0001 builds from the current
models, so on a fresh database the column already exists.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Columns added, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    ("translators", sa.Column("cities", sa.String(length=300), nullable=True)),
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


def downgrade() -> None:
    for table, column in reversed(NEW_COLUMNS):
        existing = _existing_columns(table)
        if not existing or column.name in existing:
            op.drop_column(table, str(column.name))
