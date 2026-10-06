"""suliko.ge people in Suliko Office: a link to their suliko.ge id, and no email required.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-06

People registered on suliko.ge sign in to Suliko Office with the same
credentials, and suliko.ge checks the password. Three things follow for the
schema, all on tables revision 0001 owns — so all guarded as 0006 and 0011
established: 0001 builds from today's metadata, a FRESH database already has
every one of them when this runs, an EXISTING one has none.

## accounts.suliko_user_id

The person's id on suliko.ge. Set means the password is theirs there and
`password_hash` is not used; null means an Office-only account. Unique.

## accounts.phone

A suliko.ge account can be made with a phone number and no email. This is how
such a person signs in. Unique.

## accounts.email and users.email become nullable

The same people have no address. Unique constraints still hold: PostgreSQL
treats NULLs as distinct, so any number of addressless people coexist.

## Downgrade

Drops the columns and constraints. It does NOT put NOT NULL back on the email
columns: that would fail on exactly the phone-only people this revision let in,
and nothing older cares whether the column may be empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Columns added to tables that revision 0001 owns, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    ("accounts", sa.Column("phone", sa.String(length=50), nullable=True)),
    ("accounts", sa.Column("suliko_user_id", sa.String(length=450), nullable=True)),
)

#: (constraint name, table, columns) — the names the models declare.
UNIQUE_CONSTRAINTS: tuple[tuple[str, str, list[str]], ...] = (
    ("uq_accounts_phone", "accounts", ["phone"]),
    ("uq_accounts_suliko_user_id", "accounts", ["suliko_user_id"]),
)

#: (table, column, type) of columns that stop being required.
NOW_NULLABLE: tuple[tuple[str, str, sa.types.TypeEngine[object]], ...] = (
    ("accounts", "email", sa.String(length=255)),
    ("users", "email", sa.String(length=255)),
)


def _inspector() -> sa.Inspector | None:
    bind = op.get_bind()
    return sa.inspect(bind) if bind is not None else None


def _existing_columns(table: str) -> dict[str, bool]:
    """Column name -> nullable, or nothing when there is no database (the test
    recorder): every column then looks missing, so each operation is recorded."""
    inspector = _inspector()
    if inspector is None:
        return {}
    return {column["name"]: bool(column["nullable"]) for column in inspector.get_columns(table)}


def _existing_unique(table: str) -> set[str]:
    inspector = _inspector()
    if inspector is None:
        return set()
    return {str(c["name"]) for c in inspector.get_unique_constraints(table)}


def upgrade() -> None:
    for table, column in NEW_COLUMNS:
        if column.name not in _existing_columns(table):
            op.add_column(table, column)

    for name, table, columns in UNIQUE_CONSTRAINTS:
        if name not in _existing_unique(table):
            op.create_unique_constraint(name, table, columns)

    for table, column, column_type in NOW_NULLABLE:
        # Only where it is still NOT NULL; a fresh database already has it nullable.
        if _existing_columns(table).get(column) is not True:
            op.alter_column(table, column, existing_type=column_type, nullable=True)


def downgrade() -> None:
    for name, table, _columns in reversed(UNIQUE_CONSTRAINTS):
        existing = _existing_unique(table)
        if not existing or name in existing:
            op.drop_constraint(name, table, type_="unique")
    for table, column in reversed(NEW_COLUMNS):
        columns = _existing_columns(table)
        if not columns or column.name in columns:
            op.drop_column(table, str(column.name))
