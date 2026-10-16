"""Revision 0034: the months a superuser made free."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.db.base import TenantScoped
from suliko.models import Base, BillingFreeMonth

TABLE = "billing_free_months"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0034_free_months.py")


def test_it_follows_0033_and_creates_the_one_table(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert module.down_revision == "0033"
    assert set(ops.tables) == {TABLE} == set(module.NEW_TABLES)


def test_columns_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated, declared = ops.tables[TABLE], Base.metadata.tables[TABLE]
    assert set(migrated.columns.keys()) == set(declared.columns.keys())
    for name, column in declared.columns.items():
        assert migrated.columns[name].nullable == column.nullable, name
        assert str(migrated.columns[name].type) == str(column.type), name


def test_it_is_an_accounts_own_data_with_row_level_security(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    assert issubclass(BillingFreeMonth, TenantScoped)
    sql = "\n".join(ops.statements)
    assert f"ON {TABLE} TO suliko_app" in sql
    assert f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY" in sql
    assert f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY" in sql
    assert f"CREATE POLICY tenant_isolation ON {TABLE}" in sql


def test_a_month_is_free_for_an_account_at_most_once(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    unique = {
        tuple(c.name for c in constraint.columns)
        for constraint in ops.tables[TABLE].constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("tenant_id", "period") in unique


def test_going_back_drops_the_table(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    dropped: list[str] = []

    class Ops:
        def drop_table(self, name: str) -> None:
            dropped.append(name)

    module.op = Ops()
    module.downgrade()
    assert dropped == [TABLE]
