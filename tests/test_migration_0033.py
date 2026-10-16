"""Revision 0033: packages per audience, and no per-order fee."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.domain.billing import DEFAULT_PACKAGES
from suliko.models import Base


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0033_package_audiences.py")


def test_it_creates_no_table(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert module.NEW_TABLES == () and not ops.tables


def test_the_packages_gain_an_audience_and_are_keyed_by_it(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    column = next(
        c for t, c in ops.added_columns if (t, c.name) == ("billing_packages", "audience")
    )
    declared = Base.metadata.tables["billing_packages"].columns["audience"]
    assert column.nullable is False and str(column.type) == str(declared.type)
    # Existing rows are the bureau's.
    assert str(column.server_default.arg) == "bureau"  # type: ignore[union-attr]
    assert ops.primary_keys == [("pk_billing_packages", "billing_packages", ["audience", "tier"])]
    assert {c.name for c in Base.metadata.tables["billing_packages"].primary_key.columns} == {
        "audience",
        "tier",
    }


def test_the_per_order_fee_is_gone_from_the_packages_and_the_invoices(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    assert set(ops.dropped_columns) == {
        ("billing_packages", "extra_order_cents"),
        ("billing_invoices", "extra_orders"),
        ("billing_invoices", "extra_fee_cents"),
    }
    for table, name in ops.dropped_columns:
        assert name not in Base.metadata.tables[table].columns


def test_the_freelancer_packages_are_seeded_with_what_the_code_falls_back_to(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    seeded = [s for s in ops.statements if "'freelancer'" in s]
    assert len(seeded) == 3
    for tier, (included, price) in DEFAULT_PACKAGES["freelancer"].items():
        values = f"VALUES ('freelancer', {tier}, {included}, {'NULL' if price is None else price})"
        assert any(values in s for s in seeded), values
