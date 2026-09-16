"""Per-user permission overrides, and the fields an invite carries.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-16

Adds one table and three columns.

## The three columns are the interesting part

Revision 0001 builds its tables from ``Base.metadata``, which is the models as
they stand TODAY, not as they stood when 0001 was written. So on a fresh
database 0001 already creates ``users.position``, ``users.phone`` and
``users.must_change_password`` — and a bare ``op.add_column`` here would then
fail with "column already exists", leaving a fresh database unable to reach
head. On an existing database the opposite is true and the columns must be
added.

Every earlier revision sidestepped this by only ever creating whole tables;
0004's docstring says so outright. This is the first revision that has to add
a column, so it introduces the guard: ``_add_column_if_missing`` asks the
database what is already there, exactly as 0002 and 0003 do at table
granularity with ``_existing_tables()``.

The guard also answers ``False`` when there is no real bind, which is what
lets ``tests/test_migration_0006.py`` run the upgrade against a recorder and
assert that the columns match the model.

## Why must_change_password carries a server default

``server_default=false()`` is declared on the model as well. If it were only
here, a fresh database (built by 0001 from metadata) would get the column
without a default and a migrated one would get it with — a divergence nothing
tests and nobody would notice until an INSERT that omits the column failed on
one box and not the other.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None

APP_ROLE = "suliko_app"

NEW_TABLES: tuple[str, ...] = ("user_permission_overrides",)

#: Columns added to tables that revision 0001 owns, as (table, column).
NEW_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    ("users", sa.Column("position", sa.String(length=100), nullable=True)),
    ("users", sa.Column("phone", sa.String(length=50), nullable=True)),
    (
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    ),
)


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


def _existing_columns(table: str) -> set[str]:
    """What the database already has, or nothing when there is no database.

    Returning an empty set without a bind is what makes this runnable against
    the recording stub in the tests: every column then looks missing, so the
    upgrade records an ``add_column`` for each and the test can compare them
    with the model.
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
    # No bind means the recorder, which wants to see the drop.
    if not existing or name in existing:
        op.drop_column(table, name)


def upgrade() -> None:
    for table, column in NEW_COLUMNS:
        _add_column_if_missing(table, column)

    op.create_table(
        "user_permission_overrides",
        _id(),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("permission", sa.String(length=64), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_user_permission_overrides_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_permission_overrides_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_permission_overrides"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "permission",
            name="uq_user_permission_overrides_tenant_id",
        ),
    )
    op.create_index(
        "ix_user_permission_overrides_tenant_id",
        "user_permission_overrides",
        ["tenant_id"],
    )
    op.create_index(
        "ix_user_permission_overrides_tenant_user",
        "user_permission_overrides",
        ["tenant_id", "user_id"],
    )

    # 0001's GRANT was a snapshot of the tables that existed then, so every
    # new table needs its own — and RLS on top, or it is readable across
    # every tenant.
    for table in NEW_TABLES:
        op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"))
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"""
                CREATE POLICY tenant_isolation ON {table}
                USING (
                    tenant_id = NULLIF(current_setting('suliko.tenant_id', true), '')::bigint
                )
                WITH CHECK (
                    tenant_id = NULLIF(current_setting('suliko.tenant_id', true), '')::bigint
                );
                """
            )
        )
    op.execute(sa.text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))


def downgrade() -> None:
    for table in reversed(NEW_TABLES):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    op.drop_table("user_permission_overrides")

    for table, column in reversed(NEW_COLUMNS):
        _drop_column_if_present(table, str(column.name))
