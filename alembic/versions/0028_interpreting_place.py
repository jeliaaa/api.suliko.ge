"""Where an interpreting line takes place, and when.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-14

Three nullable columns on ``order_documents``, used by oral lines only:

- ``interpreting_place``: an address as text (a courtroom, an office, a
  hospital), up to 500 characters;
- ``interpreting_date`` and ``interpreting_time``: the day and the time of day
  as the wall clock at that place, so no time zone is involved.

Null on every existing line. Same guard as 0018 to 0023 and 0026: 0001 builds
from the current models, so on a fresh database the columns exist already when
this runs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Columns added, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    ("order_documents", sa.Column("interpreting_place", sa.String(length=500), nullable=True)),
    ("order_documents", sa.Column("interpreting_date", sa.Date(), nullable=True)),
    ("order_documents", sa.Column("interpreting_time", sa.Time(), nullable=True)),
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
