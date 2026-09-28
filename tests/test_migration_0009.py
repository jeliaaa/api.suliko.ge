"""Revision 0009: email verification and a purpose on reset tokens.

Two columns on tables revision 0001 owns, so the same two failure modes as
0006 and 0008 apply — a wrong guard kills a fresh database on "already
exists", and a column that differs from the model makes fresh and migrated
databases disagree.
"""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, import_revision, load_revision
from suliko.models import Base

REVISION = "0009_email_verification.py"

ADDED = {
    ("users", "email_verified_at"),
    ("password_reset_tokens", "purpose"),
}


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(REVISION)


def test_the_added_columns_are_the_ones_the_models_gained(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    assert {(table, column.name) for table, column in ops.added_columns} == ADDED


@pytest.mark.parametrize(("table", "name"), sorted(ADDED))
def test_an_added_column_matches_the_model(
    revision: tuple[Any, RecordedOps], table: str, name: str
) -> None:
    _, ops = revision
    migrated = next(c for t, c in ops.added_columns if t == table and c.name == name)
    declared = Base.metadata.tables[table].columns[name]

    assert str(migrated.type) == str(declared.type), f"{table}.{name}: type differs"
    assert migrated.nullable == declared.nullable, f"{table}.{name}: nullability differs"


def test_purpose_has_a_matching_server_default(revision: tuple[Any, RecordedOps]) -> None:
    # NOT NULL onto a table with rows needs a default, and the model must
    # declare the same one, or fresh and migrated databases diverge.
    _, ops = revision
    migrated = next(
        c for t, c in ops.added_columns if t == "password_reset_tokens" and c.name == "purpose"
    )
    declared = Base.metadata.tables["password_reset_tokens"].columns["purpose"]

    assert migrated.server_default is not None
    assert declared.server_default is not None, "model lost its server_default"
    assert str(migrated.server_default.arg) == str(declared.server_default.arg)


def test_email_verified_at_has_no_server_default(revision: tuple[Any, RecordedOps]) -> None:
    # Nullable, so unlike `purpose` it needs none — asserting that pins the
    # column against ever silently gaining one that back-fills existing rows
    # as "verified".
    _, ops = revision
    migrated = next(
        c for t, c in ops.added_columns if t == "users" and c.name == "email_verified_at"
    )
    assert migrated.server_default is None


def test_the_guard_skips_columns_a_fresh_database_already_has() -> None:
    module = import_revision(REVISION)
    recorder = RecordedOps()
    module.op = recorder
    module._existing_columns = lambda table: {"email_verified_at", "purpose"}
    module.upgrade()

    assert recorder.added_columns == []
