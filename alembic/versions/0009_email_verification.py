"""Email verification: when it happened, and a purpose on reset tokens.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-28

No new tables. Two columns, both on tables revision 0001 owns, so both are
guarded exactly as 0006 established: 0001 builds from today's metadata, so on
a FRESH database they already exist when this runs, and on an EXISTING one
they do not.

## users.email_verified_at

Nullable, no server default. NULL means exactly one of "predates this
column" or "has not confirmed yet" — the two are indistinguishable on
purpose, and nothing here enforces anything on it. See `api/v1/auth.py`'s
`POST /auth/verify-email`.

## password_reset_tokens.purpose

The table has held one kind of token since revision 0001: a forgot-password
link, which an invite's set-password link also reuses (both prove "I received
something sent to this address" and nothing else). Email verification is a
second, distinct purpose, and the two must not clobber each other — issuing a
verification link must not spend someone's outstanding password-reset link,
or the reverse. `server_default='password_reset'` reclassifies every existing
row as what it always was, and the model declares the same default so a fresh
and a migrated database agree.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Columns added to tables that revision 0001 owns, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (
        "users",
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
    ),
    (
        "password_reset_tokens",
        sa.Column(
            "purpose",
            sa.String(length=20),
            nullable=False,
            server_default="password_reset",
        ),
    ),
)


def _existing_columns(table: str) -> set[str]:
    """What the database already has, or nothing when there is no database.

    Same contract as 0006 and 0008: with no bind (the test recorder) every
    column looks missing, so the upgrade records an ``add_column`` for each.
    """
    bind = op.get_bind()
    if bind is None:
        return set()
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _add_column_if_missing(table: str, column: sa.Column[object]) -> None:
    if column.name not in _existing_columns(table):
        op.add_column(table, column)


def _drop_column_if_present(table: str, name: str) -> None:
    existing = _existing_columns(table)
    if not existing or name in existing:
        op.drop_column(table, name)


def upgrade() -> None:
    for table, column in NEW_COLUMNS:
        _add_column_if_missing(table, column)


def downgrade() -> None:
    for table, column in reversed(NEW_COLUMNS):
        _drop_column_if_present(table, str(column.name))
