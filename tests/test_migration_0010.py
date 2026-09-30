"""Migration 0010 must build exactly what `CustomOption` declares, with RLS."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from migration_recorder import RecordedOps, load_revision
from suliko.db.base import TenantScoped
from suliko.models import Base, CustomOption

FILENAME = "0010_custom_options.py"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(FILENAME)


def test_creates_exactly_the_listed_tables(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert set(ops.tables) == set(module.NEW_TABLES) == {"custom_options"}


def test_columns_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = ops.tables["custom_options"]
    declared = Base.metadata.tables["custom_options"]
    assert set(migrated.columns.keys()) == set(declared.columns.keys())
    for name, column in declared.columns.items():
        other = migrated.columns[name]
        assert other.nullable == column.nullable, f"{name}: nullability differs"
        assert str(other.type) == str(column.type), f"{name}: type differs"


def test_indexes_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = {name for name, table, _ in ops.indexes if table == "custom_options"}
    declared = {index.name for index in Base.metadata.tables["custom_options"].indexes}
    assert migrated == declared


def test_one_value_per_list_per_bureau(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision

    def uniques(table: sa.Table) -> set[str]:
        return {
            str(c.name)
            for c in table.constraints
            if isinstance(c, sa.UniqueConstraint) and c.name is not None
        }

    assert uniques(ops.tables["custom_options"]) == uniques(Base.metadata.tables["custom_options"])


def test_the_table_is_tenant_isolated(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    sql = "\n".join(ops.statements)
    assert "ALTER TABLE custom_options ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE custom_options FORCE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY tenant_isolation ON custom_options" in sql
    assert "ON custom_options TO suliko_app" in sql
    assert issubclass(CustomOption, TenantScoped)


def test_downgrade_drops_the_table() -> None:
    module, _ = load_revision(FILENAME)
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert recorder.dropped == ["custom_options"]
