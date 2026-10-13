"""What a translator can be given: notarised work, documents, interpreting.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-13

Three flags on ``translators``:

- ``can_notarize``: may certify a translation at a notary. False for everyone
  until the bureau says otherwise.
- ``works_written``: translates documents. True for everyone, which is all
  Office has known so far.
- ``works_oral``: interprets. False for everyone.

Server defaults fill the existing rows, so the columns can be NOT NULL at once.
The guard is the one 0018 uses: 0001 builds tables from the current models, so
on a fresh database the columns already exist when this runs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Columns added, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (
        "translators",
        sa.Column("can_notarize", sa.Boolean(), server_default=sa.false(), nullable=False),
    ),
    (
        "translators",
        sa.Column("works_written", sa.Boolean(), server_default=sa.true(), nullable=False),
    ),
    (
        "translators",
        sa.Column("works_oral", sa.Boolean(), server_default=sa.false(), nullable=False),
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
