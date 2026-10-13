"""Revision 0020: the cities a translator certifies in, one nullable column."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0020_translator_cities.py")


def test_adds_only_the_cities_column(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    assert {(t, c.name) for t, c in ops.added_columns} == {("translators", "cities")}
    assert not ops.tables


def test_matches_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = next(c for _, c in ops.added_columns)
    declared = Base.metadata.tables["translators"].columns["cities"]
    assert str(migrated.type) == str(declared.type)
    assert migrated.nullable is declared.nullable is True


def test_downgrade_drops_it(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    module.downgrade()
    assert ("translators", "cities") in set(ops.dropped_columns)
