"""Revision 0021: the numbers invoices were given. One tenant table."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from migration_recorder import RecordedOps, load_revision
from suliko.db.base import TenantScoped
from suliko.models import Base

TABLE = "invoices"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0021_invoices.py")


def test_creates_the_one_table(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert set(ops.tables) == {TABLE} == set(module.NEW_TABLES)


def test_columns_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = ops.tables[TABLE]
    declared = Base.metadata.tables[TABLE]
    assert set(migrated.columns.keys()) == set(declared.columns.keys())
    for name, column in declared.columns.items():
        other = migrated.columns[name]
        assert other.nullable == column.nullable, f"{name}: nullability differs"
        assert str(other.type) == str(column.type), f"{name}: type differs"


def test_uniques_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision

    def uniques(table: sa.Table) -> set[str]:
        return {
            str(c.name)
            for c in table.constraints
            if isinstance(c, sa.UniqueConstraint) and c.name is not None
        }

    assert uniques(ops.tables[TABLE]) == uniques(Base.metadata.tables[TABLE])
    assert uniques(ops.tables[TABLE]) == {"uq_invoices_tenant_order", "uq_invoices_tenant_number"}


def test_it_is_isolated_per_tenant(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    sql = "\n".join(ops.statements)
    assert f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY" in sql
    assert f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY" in sql
    assert f"CREATE POLICY tenant_isolation ON {TABLE}" in sql
    assert f"ON {TABLE} TO suliko_app" in sql
    by_table = {mapper.local_table.name: mapper.class_ for mapper in Base.registry.mappers}
    assert issubclass(by_table[TABLE], TenantScoped)


def test_downgrade_drops_it(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert recorder.dropped == [TABLE]
