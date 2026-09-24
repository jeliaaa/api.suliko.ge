"""A bureau's claim that an invited address belongs to a suliko.ge account.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-23

One new table, ``portal_account_invites`` — platform-level, same treatment
0004 gives ``portal_translators`` and ``portal_translator_links``: granted to
``suliko_app`` and deliberately given NO row-level security, because it must
be searchable across every bureau by contact details before any tenant is
known. See ``models/portal.py`` and ``domain/portal.py`` for why, and
``api/v1/translators.py`` / ``api/v1/users.py`` for the two invite flows that
write it.

No columns are added to an existing table, so this revision does not need
0006's ``_add_column_if_missing`` guard — see that revision's docstring for
why a bare ``op.add_column`` would break a fresh database if it did.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

APP_ROLE = "suliko_app"

PLATFORM_TABLES: tuple[str, ...] = ("portal_account_invites",)
TENANT_TABLES: tuple[str, ...] = ()
NEW_TABLES: tuple[str, ...] = PLATFORM_TABLES + TENANT_TABLES


def _id() -> sa.Column[int]:
    return sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False)


def _timestamps() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
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
    )


def upgrade() -> None:
    op.create_table(
        "portal_account_invites",
        _id(),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        # VARCHAR + CHECK rather than a native enum, as in 0002 and 0004.
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("normalized_email", sa.String(length=255), nullable=True),
        sa.Column("normalized_phone", sa.String(length=32), nullable=True),
        sa.Column("translator_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("portal_translator_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("invited_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('translator', 'staff')", name="ck_portal_account_invites_invite_kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'linked', 'cancelled')",
            name="ck_portal_account_invites_invite_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_portal_account_invites_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["translator_id"],
            ["translators.id"],
            name="fk_portal_account_invites_translator_id_translators",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_portal_account_invites_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["portal_translator_id"],
            ["portal_translators.id"],
            name="fk_portal_account_invites_portal_translator_id_portal_translators",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_user_id"],
            ["users.id"],
            name="fk_portal_account_invites_invited_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_portal_account_invites"),
        sa.UniqueConstraint(
            "tenant_id", "kind", "email", name="uq_portal_account_invites_tenant_kind_email"
        ),
        sa.UniqueConstraint(
            "translator_id", name="uq_portal_account_invites_translator_id"
        ),
    )
    op.create_index(
        "ix_portal_account_invites_tenant_status", "portal_account_invites", ["tenant_id", "status"]
    )
    op.create_index(
        "ix_portal_account_invites_normalized_email",
        "portal_account_invites",
        ["normalized_email"],
    )
    op.create_index(
        "ix_portal_account_invites_normalized_phone",
        "portal_account_invites",
        ["normalized_phone"],
    )

    # ── Grants and row-level security ───────────────────────────────────────
    # Platform table: granted, but deliberately no RLS — see the module
    # docstring and models/portal.py.
    for table in NEW_TABLES:
        op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"))

    op.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))


def downgrade() -> None:
    op.drop_table("portal_account_invites")
