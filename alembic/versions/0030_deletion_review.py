"""Reviewing account-deletion requests: who decided, when, what they wrote.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-15

Six columns on ``account_deletion_requests`` (0029): when the team decided and
who (``decided_at``, ``decided_by_user_id``), the note they wrote
(``review_note``), when an approved request's data is deleted (``purge_after``,
the approval plus 30 days), when it was deleted (``done_at``), and whether
filing the request also switched off the person's translator-portal access
(``portal_deactivated``), so cancelling can switch back on exactly that.

All null or false on every existing row. One table 0029 owns, so no fresh
database builds it ahead of this; the guard below is belt and braces, as in 0018.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

TABLE = "account_deletion_requests"

#: Columns added, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (TABLE, sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True)),
    (
        TABLE,
        sa.Column(
            "decided_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "users.id",
                name="fk_account_deletion_requests_decided_by_user_id_users",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    ),
    (TABLE, sa.Column("review_note", sa.Text(), nullable=True)),
    (TABLE, sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True)),
    (TABLE, sa.Column("done_at", sa.DateTime(timezone=True), nullable=True)),
    (
        TABLE,
        sa.Column(
            "portal_deactivated", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
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
    """The review record goes; the requests themselves stay."""
    for table, column in reversed(NEW_COLUMNS):
        existing = _existing_columns(table)
        if not existing or column.name in existing:
            op.drop_column(table, str(column.name))
