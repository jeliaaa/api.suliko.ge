"""The editable text of a translation, beside the Word file it was made into.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-14

One nullable column, ``document_translations.html``. A translation from
Suliko Translate is filed as a Word document, but it is corrected on a page that
edits HTML; the HTML is kept here, so each save rebuilds the Word file from the
same source and nothing is lost to a round trip through Word. Null on every
existing translation, whose file is HTML and is edited as it is. Same guard as
0018 to 0023: 0001 builds from the current models, so on a fresh database the
column exists already when this runs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Columns added, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    ("document_translations", sa.Column("html", sa.Text(), nullable=True)),
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
