"""Revision 0014: where a translator's working copy of an order file is.

One nullable column on ``order_files``. It has to match the model, or a fresh
and a migrated database disagree.
"""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base

REVISION = "0014_order_file_working_copy.py"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(REVISION)


def test_adds_only_the_working_key(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    assert {(table, column.name) for table, column in ops.added_columns} == {
        ("order_files", "working_key")
    }
    assert not ops.tables


def test_the_column_matches_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = next(c for _, c in ops.added_columns)
    declared = Base.metadata.tables["order_files"].columns["working_key"]
    assert str(migrated.type) == str(declared.type)
    assert migrated.nullable == declared.nullable is True


def test_downgrade_drops_what_upgrade_added(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    module.downgrade()
    assert ("order_files", "working_key") in set(ops.dropped_columns)
