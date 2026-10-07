"""Order files: where a translator's working copy is.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-07

With the Order Vault as the file store (``STORAGE_BACKEND=vault``) the API
cannot read a file back, so a translator assigned to the order would get
nothing. Each file therefore also gets a plain working copy in ordinary storage
for as long as the order is open; ``order_files.working_key`` says where.

NULL means "no copy": never made, or already deleted when the order closed.
Every existing row is NULL, which is what it is: those files are in the
ordinary storage under ``storage_key`` and have no separate copy.

One nullable column on a table 0013 owns. 0013 builds ``order_files`` itself,
so the guard below is belt and braces rather than the fresh-database rescue it
is in 0006/0009.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Columns added, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    ("order_files", sa.Column("working_key", sa.String(length=500), nullable=True)),
)


def _existing_columns(table: str) -> set[str]:
    """What the database already has, or nothing when there is no database
    (the test recorder), so every column looks missing and is recorded."""
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
