"""Tenant timezone, due-date defaults, and unique payment idempotency keys.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-25

No new tables. Four columns and three indexes, all on tables revision 0001
owns — so every one of them is guarded, exactly as 0006 established: 0001
builds from today's metadata, so on a FRESH database they already exist when
this runs, and on an EXISTING one they do not.

## tenants.timezone

"Today" was ``datetime.now(UTC).date()`` everywhere: an order's default date,
what is overdue, the default finance period, the invoice date. Tbilisi is
UTC+4, so anything done between midnight and 04:00 landed on the previous
day — and on the first of a month, in the previous month's reports.

## tenant_settings.due_days_*

The PHP computes a due date from urgency (same day / +2 / +5) in its client
portal and partner API. Per tenant here, and only a default the order form
pre-fills.

## uq_*_payments_idempotency

The payment endpoints checked for an existing key and then inserted, which
lets two retries race past each other; and the notary payout never checked at
all. A unique index is the only guard that holds under concurrency. NULLs are
distinct in a unique index, so payments recorded without a key are unaffected.
``if_not_exists`` because 0001 creates them from metadata on a fresh database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: Columns added to tables that revision 0001 owns, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (
        "tenants",
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default="Asia/Tbilisi",
        ),
    ),
    (
        "tenant_settings",
        sa.Column("due_days_standard", sa.Integer(), nullable=False, server_default="5"),
    ),
    (
        "tenant_settings",
        sa.Column("due_days_express", sa.Integer(), nullable=False, server_default="2"),
    ),
    (
        "tenant_settings",
        sa.Column("due_days_urgent", sa.Integer(), nullable=False, server_default="0"),
    ),
)

#: (index name, table). Each is unique over (tenant_id, idempotency_key).
NEW_INDEXES: tuple[tuple[str, str], ...] = (
    ("uq_client_payments_idempotency", "client_payments"),
    ("uq_translator_payments_idempotency", "translator_payments"),
    ("uq_notary_payments_idempotency", "notary_payments"),
)


def _existing_columns(table: str) -> set[str]:
    """What the database already has, or nothing when there is no database.

    Same contract as 0006: with no bind (the test recorder) every column
    looks missing, so the upgrade records an ``add_column`` for each.
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

    for name, table in NEW_INDEXES:
        op.create_index(
            name,
            table,
            ["tenant_id", "idempotency_key"],
            unique=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    for name, table in reversed(NEW_INDEXES):
        op.drop_index(name, table_name=table, if_exists=True)

    for table, column in reversed(NEW_COLUMNS):
        _drop_column_if_present(table, str(column.name))
