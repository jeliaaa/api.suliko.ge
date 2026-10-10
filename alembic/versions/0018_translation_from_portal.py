"""Suliko Translate from the translator portal: who started it there.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-10

A translator can now start Suliko Translate on a file of a document assigned to
them, from suliko.ge's Orders tab. They have no Office user, so
``document_translations.requested_by_user_id`` cannot name them;
``requested_by_portal_user_id`` holds their suliko.ge account instead.

NULL means "started in Office", which is what every existing row is.

One nullable column on a table 0017 owns. The guard below is belt and braces,
as in 0014.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Columns added, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (
        "document_translations",
        sa.Column("requested_by_portal_user_id", sa.String(length=450), nullable=True),
    ),
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
