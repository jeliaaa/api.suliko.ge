"""A freelancer's answer: may other bureaus send you orders?

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-19

``tenants.accepts_bureau_orders``: asked once, when a person creates their
personal workspace. NULL means never asked, which is every bureau and every
personal workspace made before the question existed. Nothing reads it yet;
it is recorded so that routing orders from bureaus to freelancers can honour
it from the first day.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

TABLE = "tenants"
COLUMN = sa.Column("accepts_bureau_orders", sa.Boolean(), nullable=True)


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
