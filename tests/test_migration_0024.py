"""Revision 0024: pricing written work by characters. Three columns."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base

COLUMNS = {
    ("tenant_settings", "pricing_unit"),
    ("tenant_settings", "characters_per_page"),
    ("order_documents", "character_count"),
}


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0024_pricing_by_characters.py")


def test_adds_exactly_the_three_columns(revision: tuple[Any, RecordedOps]) -> None:
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
    assert migrated.nullable is declared.nullable


def test_existing_bureaus_keep_pricing_by_pages(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    unit = next(c for _, c in ops.added_columns if c.name == "pricing_unit")
    per_page = next(c for _, c in ops.added_columns if c.name == "characters_per_page")
    assert str(unit.server_default.arg) == "pages"
    assert str(per_page.server_default.arg) == "1800"


def test_downgrade_drops_them(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    module.downgrade()
    assert set(ops.dropped_columns) >= COLUMNS
