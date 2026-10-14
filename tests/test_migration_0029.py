"""Revision 0029: requests to delete an account. One platform table."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, import_revision, load_revision
from suliko.db.base import TenantScoped
from suliko.models import AccountDeletionRequest, Base

TABLE = "account_deletion_requests"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0029_account_deletion_requests.py")


def test_creates_the_one_table(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert set(ops.tables) == {TABLE} == set(module.NEW_TABLES)


def test_columns_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = ops.tables[TABLE]
    declared = Base.metadata.tables[TABLE]
    # A column 0030 added (the review record) is not 0029's.
    later = {c.name for _, c in import_revision("0030_deletion_review.py").NEW_COLUMNS}
    assert set(migrated.columns.keys()) == set(declared.columns.keys()) - later
    for name, column in declared.columns.items():
        if name in later:
            continue
        other = migrated.columns[name]
        assert other.nullable == column.nullable, f"{name}: nullability differs"
        assert str(other.type) == str(column.type), f"{name}: type differs"


def test_it_is_a_platform_table_like_accounts(revision: tuple[Any, RecordedOps]) -> None:
    """No `tenant_id`, so no policy: row-level security keys on that column.
    The grant is what the application role needs."""
    _, ops = revision
    assert "tenant_id" not in ops.tables[TABLE].columns
    assert not issubclass(AccountDeletionRequest, TenantScoped)
    sql = "\n".join(ops.statements)
    assert f"ON {TABLE} TO suliko_app" in sql
    assert "ROW LEVEL SECURITY" not in sql
    assert "CREATE POLICY" not in sql


def test_indexes_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = {name for name, table, _ in ops.indexes if table == TABLE}
    declared = {index.name for index in Base.metadata.tables[TABLE].indexes}
    assert migrated == declared


def test_only_one_request_per_account_can_be_open(revision: tuple[Any, RecordedOps]) -> None:
    """The partial unique index is what holds when two tabs press the button at once."""
    _, ops = revision
    options = ops.index_options["uq_account_deletion_requests_open"]
    assert options["unique"] is True
    where = str(options["postgresql_where"])
    assert "requested" in where
    assert "approved" in where
    # A rejected or finished request must not keep an account closed or block a new one.
    assert "rejected" not in where
    assert "done" not in where


def test_the_account_is_protected_from_deletion_while_a_request_points_at_it(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    (foreign_key,) = ops.tables[TABLE].foreign_key_constraints
    assert foreign_key.ondelete == "RESTRICT"
    assert {element.target_fullname for element in foreign_key.elements} == {"accounts.id"}


def test_downgrade_drops_the_table(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert set(recorder.dropped) == {TABLE}
