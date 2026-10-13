"""Revision 0019: what a translator can be given.

Three NOT NULL flags on ``translators`` with server defaults, so existing rows
are filled in: nobody can notarise, everybody translates documents, nobody
interprets. They have to match the model, or a fresh and a migrated database
disagree.
"""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base

REVISION = "0019_translator_capabilities.py"
COLUMNS = {
    ("translators", "can_notarize"): "false",
    ("translators", "works_written"): "true",
    ("translators", "works_oral"): "false",
}


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(REVISION)


def test_adds_only_the_three_flags(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    assert {(table, column.name) for table, column in ops.added_columns} == set(COLUMNS)
    assert not ops.tables


@pytest.mark.parametrize("key", sorted(COLUMNS))
def test_each_flag_matches_the_model(
    revision: tuple[Any, RecordedOps], key: tuple[str, str]
) -> None:
    _, ops = revision
    migrated = next(c for t, c in ops.added_columns if (t, c.name) == key)
    declared = Base.metadata.tables[key[0]].columns[key[1]]
    assert str(migrated.type) == str(declared.type)
    assert migrated.nullable is declared.nullable is False


@pytest.mark.parametrize("key", sorted(COLUMNS))
def test_existing_rows_get_the_safe_default(
    revision: tuple[Any, RecordedOps], key: tuple[str, str]
) -> None:
    """A NOT NULL column added to a table with rows needs a server default,
    and it must say what every existing translator is: documents only."""
    _, ops = revision
    migrated = next(c for t, c in ops.added_columns if (t, c.name) == key)
    assert migrated.server_default is not None
    assert str(migrated.server_default.arg).lower() == COLUMNS[key]


def test_downgrade_drops_what_upgrade_added(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    module.downgrade()
    assert set(COLUMNS) <= set(ops.dropped_columns)
