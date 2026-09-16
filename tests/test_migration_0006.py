"""Revision 0006: the first one that adds a COLUMN.

Every revision before this only ever created whole tables, which is why
`test_migration_parity.py` only knows how to check tables. A column added to a
table revision 0001 owns has a failure mode of its own, and two of them:

  - the guard is wrong, and a fresh database dies on "column already exists"
    because 0001 built the column from metadata a moment earlier;
  - the guard is right but the column is not, and the migrated database ends
    up with a type or nullability the model does not expect — which nothing
    notices until a write fails on one deployment and not the other.

Both are checked here against the recording stub, with no database.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from migration_recorder import RecordedOps, import_revision, load_revision
from suliko.models import Base

REVISION = "0006_user_invites_and_permission_overrides.py"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(REVISION)


# ── The new columns ─────────────────────────────────────────────────────────


def test_the_added_columns_are_the_ones_the_model_gained(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    assert {(table, column.name) for table, column in ops.added_columns} == {
        ("users", "position"),
        ("users", "phone"),
        ("users", "must_change_password"),
    }


@pytest.mark.parametrize("name", ["position", "phone", "must_change_password"])
def test_an_added_column_matches_the_model(revision: tuple[Any, RecordedOps], name: str) -> None:
    """Type and nullability, the same comparison the table parity test makes."""
    _, ops = revision
    migrated = next(c for table, c in ops.added_columns if table == "users" and c.name == name)
    declared = Base.metadata.tables["users"].columns[name]

    assert str(migrated.type) == str(declared.type), f"users.{name}: type differs"
    assert migrated.nullable == declared.nullable, f"users.{name}: nullability differs"


def test_a_not_null_column_carries_a_server_default(revision: tuple[Any, RecordedOps]) -> None:
    """Adding NOT NULL to a table with rows in it needs one, or the migration
    fails on any database that is not empty.

    The model declares the same default. If only the migration had it, a fresh
    database and a migrated one would end up with different DDL.
    """
    _, ops = revision
    column = next(c for _, c in ops.added_columns if c.name == "must_change_password")

    assert column.nullable is False
    assert column.server_default is not None

    declared = Base.metadata.tables["users"].columns["must_change_password"]
    assert declared.server_default is not None, (
        "the model dropped its server_default — a fresh database would then "
        "build this column without one while a migrated database has it"
    )


def test_every_added_column_exists_on_the_model(revision: tuple[Any, RecordedOps]) -> None:
    """A column added by a migration and absent from the model is a column
    nothing reads and `schema-diff` will report forever."""
    _, ops = revision
    for table, column in ops.added_columns:
        assert column.name in Base.metadata.tables[table].columns, (
            f"{table}.{column.name} is migrated but not modelled"
        )


# ── The guard that makes a fresh database work ──────────────────────────────


def test_adding_a_column_is_guarded_by_what_the_database_already_has() -> None:
    """Revision 0001 builds `users` from CURRENT metadata, so on a fresh
    database the three columns already exist by the time this revision runs.
    An unguarded `add_column` would fail and no fresh database could reach
    head."""
    module = import_revision(REVISION)

    recorder = RecordedOps()
    module.op = recorder

    # Pretend the database already has them — what `op.get_bind()` would
    # report on a fresh database built by 0001.
    module._existing_columns = lambda table: {"position", "phone", "must_change_password"}
    module.upgrade()

    assert recorder.added_columns == [], (
        "the guard did not stop the add — a fresh database cannot reach head"
    )
    # The table is still created: it is genuinely new either way.
    assert "user_permission_overrides" in recorder.tables


def test_the_guard_adds_them_when_they_are_missing() -> None:
    """The other direction: an existing database migrated from 0005 has none
    of them, and all three must arrive."""
    module = import_revision(REVISION)

    recorder = RecordedOps()
    module.op = recorder
    module._existing_columns = lambda table: set()
    module.upgrade()

    assert len(recorder.added_columns) == 3


# ── The new table ───────────────────────────────────────────────────────────


def test_the_override_table_matches_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = ops.tables["user_permission_overrides"]
    declared = Base.metadata.tables["user_permission_overrides"]

    assert set(migrated.columns.keys()) == set(declared.columns.keys())
    for name, column in declared.columns.items():
        other = migrated.columns[name]
        assert str(other.type) == str(column.type), f"{name}: type differs"
        assert other.nullable == column.nullable, f"{name}: nullability differs"


def test_the_override_table_is_tenant_isolated(revision: tuple[Any, RecordedOps]) -> None:
    """It names permissions per user. Readable across tenants, it would leak
    who can do what in every other bureau."""
    module, ops = revision
    sql = "\n".join(ops.statements)

    for table in module.NEW_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY tenant_isolation ON {table}" in sql
        assert f"ON {table} TO suliko_app" in sql


def test_one_row_per_user_and_permission(revision: tuple[Any, RecordedOps]) -> None:
    """Without the unique constraint, an edit that wrote a second row would
    leave the effective set depending on which one was read first."""
    _, ops = revision
    table = ops.tables["user_permission_overrides"]

    unique = [c for c in table.constraints if isinstance(c, sa.UniqueConstraint)]
    assert any(
        {col.name for col in c.columns} == {"tenant_id", "user_id", "permission"} for c in unique
    )


def test_downgrade_reverses_the_upgrade(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision

    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()

    assert set(recorder.dropped) == set(module.NEW_TABLES)
    assert {(t, n) for t, n in recorder.dropped_columns} == {
        ("users", "position"),
        ("users", "phone"),
        ("users", "must_change_password"),
    }
