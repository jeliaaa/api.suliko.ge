"""Revision 0036: a freelancer's answer to "may bureaus send you orders?"."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0036_accepts_bureau_orders.py")


def test_adds_one_nullable_column_to_tenants(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert [(t, c.name) for t, c in ops.added_columns] == [("tenants", "accepts_bureau_orders")]
    assert not ops.tables
    assert module.NEW_TABLES == ()
    migrated = ops.added_columns[0][1]
    declared = Base.metadata.tables["tenants"].columns["accepts_bureau_orders"]
    assert str(migrated.type) == str(declared.type)
    assert migrated.nullable is True


def test_downgrade_drops_it(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert recorder.dropped_columns == [("tenants", "accepts_bureau_orders")]
