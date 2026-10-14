"""Revision 0028: where an interpreting line takes place, and when."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base

COLUMNS = {
    ("order_documents", "interpreting_place"),
    ("order_documents", "interpreting_date"),
    ("order_documents", "interpreting_time"),
}


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0028_interpreting_place.py")


def test_adds_the_three_columns_only(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    assert {(t, c.name) for t, c in ops.added_columns} == COLUMNS
    assert not ops.tables


@pytest.mark.parametrize("key", sorted(COLUMNS))
def test_each_column_matches_the_model(
    revision: tuple[Any, RecordedOps], key: tuple[str, str]
) -> None:
    _, ops = revision
    migrated = next(c for t, c in ops.added_columns if (t, c.name) == key)
    declared = Base.metadata.tables[key[0]].columns[key[1]]
    assert str(migrated.type) == str(declared.type)
    assert migrated.nullable is declared.nullable is True


def test_downgrade_drops_them(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    module.downgrade()
    assert set(ops.dropped_columns) >= COLUMNS
