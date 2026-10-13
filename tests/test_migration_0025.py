"""Revision 0025: the record of spreadsheet imports. Two tenant tables."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.db.base import TenantScoped
from suliko.models import Base

TABLES = {"import_batches", "import_batch_items"}


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0025_imports.py")


def test_creates_the_two_tables(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert set(ops.tables) == TABLES == set(module.NEW_TABLES)


@pytest.mark.parametrize("table", sorted(TABLES))
def test_columns_match_the_model(revision: tuple[Any, RecordedOps], table: str) -> None:
    _, ops = revision
    migrated = ops.tables[table]
    declared = Base.metadata.tables[table]
    assert set(migrated.columns.keys()) == set(declared.columns.keys())
    for name, column in declared.columns.items():
        other = migrated.columns[name]
        assert other.nullable == column.nullable, f"{table}.{name}: nullability differs"
        assert str(other.type) == str(column.type), f"{table}.{name}: type differs"


@pytest.mark.parametrize("table", sorted(TABLES))
def test_each_table_is_isolated_per_tenant(revision: tuple[Any, RecordedOps], table: str) -> None:
    _, ops = revision
    sql = "\n".join(ops.statements)
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
    assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
    assert f"CREATE POLICY tenant_isolation ON {table}" in sql
    assert f"ON {table} TO suliko_app" in sql
    by_table = {mapper.local_table.name: mapper.class_ for mapper in Base.registry.mappers}
    assert issubclass(by_table[table], TenantScoped)


def test_indexes_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    for table in TABLES:
        migrated = {name for name, t, _ in ops.indexes if t == table}
        declared = {index.name for index in Base.metadata.tables[table].indexes}
        assert migrated == declared, table


def test_downgrade_drops_both(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    from migration_recorder import RecordedOps as Recorder

    recorder = Recorder()
    module.op = recorder
    module.downgrade()
    assert set(recorder.dropped) == TABLES
