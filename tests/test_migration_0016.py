"""Revision 0016: a per-tenant ``number`` on orders and clients.

Columns, a backfill, constraints and a trigger on tables revision 0001 owns,
so all guarded: a fresh database already has the columns and constraints
from 0001 (built from today's models), an existing one has none of them.
"""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base

REVISION = "0016_per_tenant_numbers.py"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(REVISION)


def test_adds_a_number_to_orders_and_clients(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    assert {(table, column.name) for table, column in ops.added_columns} == {
        ("orders", "number"),
        ("clients", "number"),
    }
    assert not ops.tables


def test_the_column_ends_as_the_model_declares(revision: tuple[Any, RecordedOps]) -> None:
    """Added nullable for the backfill, then made required, as the model is."""
    _, ops = revision
    for table, column in ops.added_columns:
        declared = Base.metadata.tables[table].columns["number"]
        assert str(column.type) == str(declared.type)
        assert column.nullable is True
        assert (table, "number", False) in ops.altered_columns
        assert declared.nullable is False


def test_the_unique_constraints_carry_the_models_names(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    declared = {
        constraint.name
        for table in ("orders", "clients")
        for constraint in Base.metadata.tables[table].constraints
        if constraint.name and constraint.name.startswith("uq_")
    }
    assert {name for name, _table, _columns in ops.unique_constraints} == declared
    assert all(columns == ["tenant_id", "number"] for _n, _t, columns in ops.unique_constraints)


def test_the_backfill_runs_per_tenant_under_row_level_security(
    revision: tuple[Any, RecordedOps],
) -> None:
    """FORCE ROW LEVEL SECURITY binds the owner too; the backfill sets the tenant."""
    _, ops = revision
    backfills = [s for s in ops.statements if "row_number()" in s]
    assert len(backfills) == 2
    for statement in backfills:
        assert "set_config('suliko.tenant_id', t.id::text, true)" in statement
        assert "ORDER BY id" in statement


def test_a_trigger_numbers_rows_inserted_around_the_orm(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    joined = "\n".join(ops.statements)
    assert "pg_advisory_xact_lock" in joined
    for table in ("orders", "clients"):
        assert f"CREATE TRIGGER {table}_assign_number BEFORE INSERT ON {table}" in joined


def test_downgrade_removes_what_upgrade_added(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    module.downgrade()
    assert {("orders", "number"), ("clients", "number")} <= set(ops.dropped_columns)
    assert {name for name, _table in ops.dropped_constraints} == {
        "uq_orders_tenant_number",
        "uq_clients_tenant_number",
    }
    assert any("DROP FUNCTION IF EXISTS" in s for s in ops.statements)
