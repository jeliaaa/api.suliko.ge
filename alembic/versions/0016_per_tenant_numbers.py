"""Orders and clients get a number of their own in each bureau.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-10

Suliko Office showed the database id as an order's or client's number. Ids are
shared by every bureau, so a new freelancer's third order was "#13" and their
first client "#12" (Suliko Office UX audit, F41). From here on each bureau
counts its own: `orders.number` and `clients.number`, 1, 2, 3, unique within a
tenant. The id stays the key, in every URL and every foreign key.

## Existing rows are renumbered

Decided 2026-10-10: every bureau's orders and clients are numbered 1, 2, 3 in
the order they were created (by id), per tenant. The numbers people knew
before (the ids) stop being shown.

## New rows

A BEFORE INSERT trigger fills `number` with the tenant's highest plus one,
under a transaction-level advisory lock per (table, tenant), so two orders
created at the same moment in one bureau never draw the same number. A
trigger rather than application code, so every way in (the API, the user
import script, a manual INSERT) is numbered the same. The ORM leaves the
column out of its INSERT and reads it back (`eager_defaults`).

Row-level security does not get in the way: the trigger's SELECT sees the
inserting tenant's rows, which are the ones it needs.

## Fresh databases

Revision 0001 builds `orders` and `clients` from today's models, so on a fresh
database the columns and constraints already exist and only the function and
triggers are added. Guarded the way 0006, 0011 and 0012 are.

## Downgrade

Drops the triggers, the function, the constraints and the columns. The ids are
still there, so nothing is lost but the numbers.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None

#: No tables. Listed so the parity tests' chain checks see this revision.
NEW_TABLES: tuple[str, ...] = ()

#: The tables that get a per-tenant number.
NUMBERED: tuple[str, ...] = ("orders", "clients")

#: (constraint name, table, columns) — the names the models declare.
UNIQUE_CONSTRAINTS: tuple[tuple[str, str, list[str]], ...] = (
    ("uq_orders_tenant_number", "orders", ["tenant_id", "number"]),
    ("uq_clients_tenant_number", "clients", ["tenant_id", "number"]),
)

FUNCTION = "suliko_assign_tenant_number"

CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {FUNCTION}() RETURNS trigger AS $$
BEGIN
    IF NEW.number IS NULL THEN
        -- One numberer at a time per table and tenant; released at commit.
        PERFORM pg_advisory_xact_lock(
            hashtextextended(TG_TABLE_NAME || ':' || NEW.tenant_id::text, 0)
        );
        EXECUTE format(
            'SELECT coalesce(max(number), 0) + 1 FROM %I WHERE tenant_id = $1',
            TG_TABLE_NAME
        ) INTO NEW.number USING NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def backfill(table: str) -> str:
    """1, 2, 3 per tenant, in the order the rows were created.

    The tables have FORCE ROW LEVEL SECURITY, which binds the owner too: a
    migration role that is not a superuser sees no rows at all. So, as 0011
    does, walk the tenants and set ``suliko.tenant_id`` for each.
    """
    return f"""
DO $$
DECLARE
    t RECORD;
BEGIN
    FOR t IN SELECT id FROM tenants ORDER BY id LOOP
        PERFORM set_config('suliko.tenant_id', t.id::text, true);
        UPDATE {table} AS target
        SET number = ranked.rn
        FROM (
            SELECT id, row_number() OVER (ORDER BY id) AS rn
            FROM {table}
            WHERE tenant_id = t.id
        ) AS ranked
        WHERE target.id = ranked.id;
    END LOOP;
    PERFORM set_config('suliko.tenant_id', '', true);
END $$;
"""


def _trigger(table: str) -> str:
    return f"{table}_assign_number"


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
    for table in NUMBERED:
        columns = _existing_columns(table)
        if "number" not in columns:
            op.add_column(table, sa.Column("number", sa.Integer(), nullable=True))
            op.execute(sa.text(backfill(table)))
        if _existing_columns(table).get("number") is not False:
            op.alter_column(table, "number", existing_type=sa.Integer(), nullable=False)

    for name, table, columns in UNIQUE_CONSTRAINTS:
        if name not in _existing_unique(table):
            op.create_unique_constraint(name, table, columns)

    op.execute(sa.text(CREATE_FUNCTION))
    for table in NUMBERED:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_trigger(table)} ON {table}"))
        op.execute(
            sa.text(
                f"CREATE TRIGGER {_trigger(table)} BEFORE INSERT ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()"
            )
        )


def downgrade() -> None:
    for table in NUMBERED:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_trigger(table)} ON {table}"))
    op.execute(sa.text(f"DROP FUNCTION IF EXISTS {FUNCTION}()"))
    for name, table, _columns in reversed(UNIQUE_CONSTRAINTS):
        existing = _existing_unique(table)
        if not existing or name in existing:
            op.drop_constraint(name, table, type_="unique")
    for table in reversed(NUMBERED):
        columns = _existing_columns(table)
        if not columns or "number" in columns:
            op.drop_column(table, "number")
