"""Pricing written work by characters: a bureau setting and a count per line.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-13

- ``tenant_settings.pricing_unit``: 'pages' (every bureau today) or 'characters'.
- ``tenant_settings.characters_per_page``: how many characters make one standard
  page, 1800 unless the bureau says otherwise.
- ``order_documents.character_count``: characters with spaces, null for every
  existing line (they were priced by pages and stay so).

Same guard as 0018 to 0023: 0001 builds from the current models, so on a fresh
database the columns exist already when this runs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Columns added, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (
        "tenant_settings",
        sa.Column("pricing_unit", sa.String(length=10), server_default="pages", nullable=False),
    ),
    (
        "tenant_settings",
        sa.Column("characters_per_page", sa.Integer(), server_default="1800", nullable=False),
    ),
    ("order_documents", sa.Column("character_count", sa.Integer(), nullable=True)),
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
