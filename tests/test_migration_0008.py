"""Revision 0008: tenant timezone, due-date defaults, payment idempotency.

Columns and indexes on tables revision 0001 owns, so the same two failure
modes as 0006 apply — a guard that is wrong kills a fresh database on "already
exists", and a column that differs from the model makes fresh and migrated
databases disagree. Both are checked against the recording stub.
"""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, import_revision, load_revision
from suliko.models import Base

REVISION = "0008_tenant_timezone_and_money_guards.py"

ADDED = {
    ("tenants", "timezone"),
    ("tenant_settings", "due_days_standard"),
    ("tenant_settings", "due_days_express"),
    ("tenant_settings", "due_days_urgent"),
}


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(REVISION)


def test_the_added_columns_are_the_ones_the_models_gained(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    assert {(table, column.name) for table, column in ops.added_columns} == ADDED


@pytest.mark.parametrize(("table", "name"), sorted(ADDED))
def test_an_added_column_matches_the_model(
    revision: tuple[Any, RecordedOps], table: str, name: str
) -> None:
    _, ops = revision
    migrated = next(c for t, c in ops.added_columns if t == table and c.name == name)
    declared = Base.metadata.tables[table].columns[name]

    assert str(migrated.type) == str(declared.type), f"{table}.{name}: type differs"
    assert migrated.nullable == declared.nullable, f"{table}.{name}: nullability differs"
    # NOT NULL onto a table with rows needs a default — and the model must
    # declare the same one, or fresh and migrated databases diverge.
    assert migrated.server_default is not None
    assert declared.server_default is not None, f"{table}.{name}: model lost server_default"
    assert str(migrated.server_default.arg) == str(declared.server_default.arg)


def test_the_guard_skips_columns_a_fresh_database_already_has() -> None:
    module = import_revision(REVISION)
    recorder = RecordedOps()
    module.op = recorder
    module._existing_columns = lambda table: {
        "timezone",
        "due_days_standard",
        "due_days_express",
        "due_days_urgent",
    }
    module.upgrade()

    assert recorder.added_columns == []


@pytest.mark.parametrize(
    "table", ["client_payments", "translator_payments", "notary_payments"]
)
def test_each_payment_ledger_gets_a_unique_idempotency_index(
    revision: tuple[Any, RecordedOps], table: str
) -> None:
    _, ops = revision
    name = f"uq_{table}_idempotency"
    assert (name, table, ["tenant_id", "idempotency_key"]) in ops.indexes
    assert ops.index_options[name].get("unique") is True
    # 0001 builds these from metadata on a fresh database.
    assert ops.index_options[name].get("if_not_exists") is True

    declared = {index.name: index for index in Base.metadata.tables[table].indexes}
    assert name in declared and declared[name].unique, (
        f"{name} is migrated but the model does not declare it unique"
    )
