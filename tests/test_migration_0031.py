"""Revision 0031: order packages, their invoices, and each bureau's package."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.db.base import TenantScoped
from suliko.domain.billing import DEFAULT_PACKAGES
from suliko.models import Base, BillingInvoice, BillingPackage

PACKAGES, INVOICES = "billing_packages", "billing_invoices"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0031_order_packages.py")


def test_creates_the_two_tables(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert set(ops.tables) == {PACKAGES, INVOICES} == set(module.NEW_TABLES)


@pytest.mark.parametrize("table", [PACKAGES, INVOICES])
def test_columns_match_the_model(revision: tuple[Any, RecordedOps], table: str) -> None:
    _, ops = revision
    migrated = ops.tables[table]
    declared = Base.metadata.tables[table]
    assert set(migrated.columns.keys()) == set(declared.columns.keys())
    for name, column in declared.columns.items():
        other = migrated.columns[name]
        assert other.nullable == column.nullable, f"{table}.{name}: nullability differs"
        assert str(other.type) == str(column.type), f"{table}.{name}: type differs"


def test_the_packages_are_a_platform_table_and_the_invoices_are_a_bureaus_own(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    assert "tenant_id" not in ops.tables[PACKAGES].columns
    assert not issubclass(BillingPackage, TenantScoped)
    assert issubclass(BillingInvoice, TenantScoped)
    sql = "\n".join(ops.statements)
    assert f"ON {PACKAGES} TO suliko_app" in sql
    assert f"ON {INVOICES} TO suliko_app" in sql
    # Row-level security on the invoices only: a bureau sees its own.
    assert f"ALTER TABLE {INVOICES} ENABLE ROW LEVEL SECURITY" in sql
    assert f"ALTER TABLE {INVOICES} FORCE ROW LEVEL SECURITY" in sql
    assert f"CREATE POLICY tenant_isolation ON {INVOICES}" in sql
    assert f"ALTER TABLE {PACKAGES}" not in sql


def test_a_bureau_has_one_invoice_a_month(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in ops.tables[INVOICES].constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("tenant_id", "period") in unique


def test_the_packages_start_with_the_numbers_the_code_falls_back_to(
    revision: tuple[Any, RecordedOps],
) -> None:
    """Placeholder numbers a superuser changes in the admin console."""
    _, ops = revision
    seeded = [s for s in ops.statements if s.startswith("INSERT INTO billing_packages")]
    assert len(seeded) == len(DEFAULT_PACKAGES) == 3
    for tier, (included, price, extra) in DEFAULT_PACKAGES.items():
        values = f"VALUES ({tier}, {included}, {'NULL' if price is None else price}, {extra})"
        assert any(values in statement for statement in seeded), values


def test_every_bureau_that_exists_starts_on_package_one(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    ((table, column),) = ops.added_columns
    assert (table, column.name) == ("tenants", "package_tier")
    declared = Base.metadata.tables["tenants"].columns["package_tier"]
    assert column.nullable is False and declared.nullable is False
    assert str(column.type) == str(declared.type)
    assert str(column.server_default.arg) == "1"  # type: ignore[union-attr]
    assert str(declared.server_default.arg) == "1"  # type: ignore[union-attr]
