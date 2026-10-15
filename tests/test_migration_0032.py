"""Revision 0032: a bureau's own invoice file."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.db.base import TenantScoped
from suliko.models import Base, InvoiceTemplate

TABLE = "invoice_templates"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0032_invoice_templates.py")


def test_creates_the_one_table(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert set(ops.tables) == {TABLE} == set(module.NEW_TABLES)


def test_columns_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated, declared = ops.tables[TABLE], Base.metadata.tables[TABLE]
    assert set(migrated.columns.keys()) == set(declared.columns.keys())
    for name, column in declared.columns.items():
        assert migrated.columns[name].nullable == column.nullable, name
        assert str(migrated.columns[name].type) == str(column.type), name


def test_it_is_a_bureaus_own_data_with_row_level_security(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    assert issubclass(InvoiceTemplate, TenantScoped)
    sql = "\n".join(ops.statements)
    assert f"ON {TABLE} TO suliko_app" in sql
    assert f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY" in sql
    assert f"CREATE POLICY tenant_isolation ON {TABLE}" in sql


def test_a_bureau_has_at_most_one(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    unique = {
        tuple(c.name for c in constraint.columns)
        for constraint in ops.tables[TABLE].constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("tenant_id",) in unique
