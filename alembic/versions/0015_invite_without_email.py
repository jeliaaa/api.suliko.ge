"""A translator invite no longer needs an email address.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-09

A translator who signs in to suliko.ge with a phone number has no address. The
bureau finds them by that number and picks their account, and nothing is
emailed: they simply see the bureau in their Orders tab. The invite row that
records this therefore has no email to put in `portal_account_invites.email`.

Only the NOT NULL goes. The unique constraint on (tenant, kind, email) stays:
PostgreSQL treats NULLs as distinct, so any number of addressless invites
coexist, and two invites for the same ADDRESS still collide.

`portal_account_invites` is built by revision 0007 from its own definition, not
from the models (it is in 0001's LATER_REVISION_TABLES), so on a fresh database
the column is NOT NULL when this runs, exactly as on an existing one. The guard
only skips a database where it is already nullable.

## Downgrade

Does not put NOT NULL back: that would fail on exactly the phone-only invites
this revision allowed, and nothing older cares whether the column may be empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: (table, column, type) of columns that stop being required.
NOW_NULLABLE: tuple[tuple[str, str, sa.types.TypeEngine[object]], ...] = (
    ("portal_account_invites", "email", sa.String(length=255)),
)


def _nullable(table: str, column: str) -> bool | None:
    """Whether the column is nullable now, or None when there is no database
    (the test recorder), so the change is recorded."""
    bind = op.get_bind()
    if bind is None:
        return None
    for row in sa.inspect(bind).get_columns(table):
        if row["name"] == column:
            return bool(row["nullable"])
    return None


def upgrade() -> None:
    for table, column, column_type in NOW_NULLABLE:
        if _nullable(table, column) is not True:
            op.alter_column(table, column, existing_type=column_type, nullable=True)


def downgrade() -> None:
    # Deliberately empty — see the module docstring.
    pass
