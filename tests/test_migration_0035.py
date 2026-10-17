"""Revision 0035: the first-run tips a person has dismissed."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0035_account_tips.py")


def test_adds_one_column_to_accounts(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert [(t, c.name) for t, c in ops.added_columns] == [("accounts", "tips_seen")]
    assert not ops.tables
    assert module.NEW_TABLES == ()


def test_the_column_matches_the_model_and_starts_empty(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = ops.added_columns[0][1]
    declared = Base.metadata.tables["accounts"].columns["tips_seen"]
    assert str(migrated.type) == str(declared.type)
    assert migrated.nullable is False
    assert migrated.server_default is not None


def test_downgrade_drops_it(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert recorder.dropped_columns == [("accounts", "tips_seen")]
