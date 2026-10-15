"""Requests to delete an account.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-15

``account_deletion_requests``: who asked (the account), when (``created_at``) and
where the request stands (requested, approved, rejected, done). A person presses
"Delete account" on their Account page; while a request is open the account
cannot sign in anywhere, and the team reviews it.

A platform table, like ``accounts``: the request is about the person, not one
organisation, so there is no ``tenant_id`` and no row-level security — only the
grant to the application role.

At most one request per account is open at a time (a partial unique index);
older ones, rejected or done, stay as the record.

Review notes, the deciding person and the purge date arrive with the review
screen, which is when they are first written.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "suliko_app"

NEW_TABLES: tuple[str, ...] = ("account_deletion_requests",)
#: Tables that carry no ``tenant_id`` and so get no policy.
PLATFORM_TABLES: tuple[str, ...] = NEW_TABLES


def upgrade() -> None:
    op.create_table(
        "account_deletion_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_account_deletion_requests_account_id_accounts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_account_deletion_requests"),
    )
    op.create_index(
        "ix_account_deletion_requests_account_id", "account_deletion_requests", ["account_id"]
    )
    op.create_index(
        "uq_account_deletion_requests_open",
        "account_deletion_requests",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('requested', 'approved')"),
    )

    op.execute(
        sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON account_deletion_requests TO {APP_ROLE}")
    )
    op.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))


def downgrade() -> None:
    """The requests go, and with them the reason any account was closed: a person
    whose request was open can sign in again."""
    op.drop_table("account_deletion_requests")
