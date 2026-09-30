"""One account per person: email and password move out of each organisation.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-30

Until now every organisation stored its own password for a user, so the same
person had as many passwords as bureaus. This adds ``accounts`` (email,
password, verified address — platform-level, no RLS: sign-in must find it
before any organisation is known) and links each ``users`` row to one.

## Why ``accounts`` is not in NEW_TABLES

``users.account_id`` is a foreign key to ``accounts``, and revision 0001 builds
``users`` from the CURRENT models. On a fresh database it must therefore build
``accounts`` too, or the foreign key has nothing to point at — so ``accounts``
is left to 0001 (it is not in 0001's LATER_REVISION_TABLES), and this revision
creates it only where it is missing: every database that already existed.
Same table-level guard as 0002/0003, same column-level guard as 0006.

## The backfill

One account per lower-cased email. Where one person had different passwords
in different organisations, the password (and name, and must-change flag) of
the membership they signed in to most recently wins — decided 2026-09-30.
Anyone who types an older one uses "Forgot password".

``users`` has FORCE ROW LEVEL SECURITY, which binds the table owner too; a
migration role that is not a superuser would see no rows at all. So the
backfill walks the tenants and sets ``suliko.tenant_id`` for each, the way the
application does, and relies on ON CONFLICT to keep the most recent password
as later tenants are visited.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None

APP_ROLE = "suliko_app"

#: Tables this revision owns outright. Empty — see "Why accounts is not in
#: NEW_TABLES" above.
NEW_TABLES: tuple[str, ...] = ()

#: Created here only when missing; a fresh database already has it from 0001.
GUARDED_TABLES: tuple[str, ...] = ("accounts",)

NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    (
        "users",
        sa.Column(
            "account_id",
            sa.BigInteger(),
            sa.ForeignKey("accounts.id", name="fk_users_account_id_accounts", ondelete="RESTRICT"),
            nullable=True,
        ),
    ),
    (
        "users",
        sa.Column("invitation_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    ),
    (
        "tenants",
        sa.Column("is_personal", sa.Boolean(), nullable=False, server_default=sa.false()),
    ),
)

BACKFILL = """
DO $$
DECLARE
    t RECORD;
BEGIN
    FOR t IN SELECT id FROM tenants ORDER BY id LOOP
        PERFORM set_config('suliko.tenant_id', t.id::text, true);

        INSERT INTO accounts (
            email, password_hash, full_name, must_change_password,
            email_verified_at, last_login_at, created_at, updated_at
        )
        SELECT DISTINCT ON (lower(u.email))
            lower(u.email), u.password_hash, u.full_name, u.must_change_password,
            u.email_verified_at, u.last_login_at, now(), now()
        FROM users u
        WHERE u.tenant_id = t.id AND btrim(u.email) <> ''
        ORDER BY lower(u.email), u.last_login_at DESC NULLS LAST, u.id DESC
        ON CONFLICT (email) DO UPDATE SET
            password_hash = CASE
                WHEN EXCLUDED.last_login_at IS NOT NULL AND (
                    accounts.last_login_at IS NULL
                    OR EXCLUDED.last_login_at > accounts.last_login_at
                ) THEN EXCLUDED.password_hash ELSE accounts.password_hash END,
            must_change_password = CASE
                WHEN EXCLUDED.last_login_at IS NOT NULL AND (
                    accounts.last_login_at IS NULL
                    OR EXCLUDED.last_login_at > accounts.last_login_at
                ) THEN EXCLUDED.must_change_password ELSE accounts.must_change_password END,
            full_name = CASE
                WHEN EXCLUDED.last_login_at IS NOT NULL AND (
                    accounts.last_login_at IS NULL
                    OR EXCLUDED.last_login_at > accounts.last_login_at
                ) THEN EXCLUDED.full_name ELSE accounts.full_name END,
            last_login_at = GREATEST(accounts.last_login_at, EXCLUDED.last_login_at),
            email_verified_at = COALESCE(accounts.email_verified_at, EXCLUDED.email_verified_at);

        UPDATE users u
        SET account_id = a.id
        FROM accounts a
        WHERE u.tenant_id = t.id AND u.account_id IS NULL AND a.email = lower(u.email);
    END LOOP;

    PERFORM set_config('suliko.tenant_id', '', true);
END $$;
"""


def _inspector() -> sa.Inspector | None:
    bind = op.get_bind()
    return sa.inspect(bind) if bind is not None else None


def _existing_tables() -> set[str]:
    inspector = _inspector()
    return set(inspector.get_table_names()) if inspector is not None else set()


def _existing_columns(table: str) -> set[str]:
    inspector = _inspector()
    if inspector is None:
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _existing_indexes(table: str) -> set[str]:
    inspector = _inspector()
    if inspector is None:
        return set()
    return {str(index["name"]) for index in inspector.get_indexes(table)}


def upgrade() -> None:
    if "accounts" not in _existing_tables():
        op.create_table(
            "accounts",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("email", sa.String(length=255), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("full_name", sa.String(length=255), nullable=False),
            sa.Column(
                "must_change_password",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
            sa.PrimaryKeyConstraint("id", name="pk_accounts"),
            sa.UniqueConstraint("email", name="uq_accounts_email"),
        )

    for table, column in NEW_COLUMNS:
        if column.name not in _existing_columns(table):
            op.add_column(table, column)

    if "ix_users_account_id" not in _existing_indexes("users"):
        op.create_index("ix_users_account_id", "users", ["account_id"])

    op.execute(sa.text(BACKFILL))

    # 0001's grant was a snapshot of the tables that existed then. Platform
    # table: granted, deliberately no RLS — see the model's docstring.
    op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON accounts TO {APP_ROLE}"))
    op.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))


def downgrade() -> None:
    existing = _existing_indexes("users")
    if not existing or "ix_users_account_id" in existing:
        op.drop_index("ix_users_account_id", table_name="users")
    for table, column in reversed(NEW_COLUMNS):
        columns = _existing_columns(table)
        if not columns or column.name in columns:
            op.drop_column(table, str(column.name))
    op.drop_table("accounts")
