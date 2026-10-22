"""Revision 0037: a freelancer's rate for orders that come from a bureau."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0037_pair_price_bureau.py")


def test_adds_one_nullable_column_to_pair_prices(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert [(t, c.name) for t, c in ops.added_columns] == [
        ("language_pair_prices", "price_per_page_bureau")
    ]
    assert not ops.tables
    assert module.NEW_TABLES == ()
    migrated = ops.added_columns[0][1]
    declared = Base.metadata.tables["language_pair_prices"].columns["price_per_page_bureau"]
    assert str(migrated.type) == str(declared.type)
    assert migrated.nullable is True


def test_downgrade_drops_it(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert recorder.dropped_columns == [("language_pair_prices", "price_per_page_bureau")]
