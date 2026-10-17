"""Which first-run tips a person has seen.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-17

``accounts.tips_seen``: a JSON list of tip ids the person has dismissed (or
finished in the tour). Per account, not per organisation: the tips explain the
product, and a person who knows it does not need them again in a second
organisation. Existing rows start with an empty list; whether tips show at all
is decided by the account's age (`TIPS_ACCOUNTS_SINCE`), not by this column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

TABLE = "accounts"
COLUMN = sa.Column("tips_seen", sa.JSON(), server_default=sa.text("'[]'"), nullable=False)


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
