"""Revision 0030: the review record on account-deletion requests."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base

TABLE = "account_deletion_requests"
COLUMNS = {
    "decided_at",
    "decided_by_user_id",
    "review_note",
    "purge_after",
    "done_at",
    "portal_deactivated",
}


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0030_deletion_review.py")


def test_adds_the_six_columns_only(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert {(t, c.name) for t, c in ops.added_columns} == {(TABLE, name) for name in COLUMNS}
    assert not ops.tables
    assert module.NEW_TABLES == ()


@pytest.mark.parametrize("name", sorted(COLUMNS))
def test_each_column_matches_the_model(revision: tuple[Any, RecordedOps], name: str) -> None:
    _, ops = revision
    migrated = next(c for _, c in ops.added_columns if c.name == name)
    declared = Base.metadata.tables[TABLE].columns[name]
    assert str(migrated.type) == str(declared.type)
    assert migrated.nullable is declared.nullable


def test_the_deciding_user_is_kept_if_that_user_goes(revision: tuple[Any, RecordedOps]) -> None:
    """SET NULL: the request is the record and must outlive the reviewer's row."""
    _, ops = revision
    column = next(c for _, c in ops.added_columns if c.name == "decided_by_user_id")
    (foreign_key,) = column.foreign_keys
    assert foreign_key.ondelete == "SET NULL"
    assert foreign_key.target_fullname == "users.id"


def test_existing_rows_start_without_a_switched_off_portal(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    column = next(c for _, c in ops.added_columns if c.name == "portal_deactivated")
    assert column.nullable is False
    assert column.server_default is not None


def test_downgrade_drops_them(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert {name for _, name in recorder.dropped_columns} == COLUMNS
