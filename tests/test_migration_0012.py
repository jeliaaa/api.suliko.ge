"""Migration 0012 — the suliko.ge link on accounts, and email no longer required.

No database: the revision runs against the recorder, where every column and
constraint looks missing and every NOT NULL looks still in force, so each
guarded operation is recorded and compared with the models.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import UniqueConstraint

from migration_recorder import RecordedOps, load_revision
from suliko.models import Account, User

FILENAME = "0012_suliko_accounts.py"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(FILENAME)


@pytest.mark.parametrize("column", ["phone", "suliko_user_id"])
def test_added_columns_match_the_model(revision: tuple[Any, RecordedOps], column: str) -> None:
    _, ops = revision
    migrated = {(t, c.name): c for t, c in ops.added_columns}[("accounts", column)]
    declared = Account.__table__.c[column]
    assert migrated.nullable == declared.nullable
    assert str(migrated.type) == str(declared.type)
    assert (migrated.server_default is None) == (declared.server_default is None)


def test_the_unique_constraints_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    declared = {
        constraint.name: [column.name for column in constraint.columns]
        for constraint in Account.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    for name, table, columns in ops.unique_constraints:
        assert table == "accounts"
        assert declared[name] == columns
    assert {name for name, _t, _c in ops.unique_constraints} == {
        "uq_accounts_phone",
        "uq_accounts_suliko_user_id",
    }


@pytest.mark.parametrize("model", [Account, User])
def test_email_is_optional_in_the_models_and_in_the_migration(
    revision: tuple[Any, RecordedOps], model: Any
) -> None:
    _, ops = revision
    assert model.__table__.c.email.nullable is True
    assert (model.__tablename__, "email", True) in ops.altered_columns


def test_the_email_type_is_unchanged(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    types = {(t, c): str(ty) for t, c, ty in module.NOW_NULLABLE}
    assert types[("accounts", "email")] == str(Account.__table__.c.email.type)
    assert types[("users", "email")] == str(User.__table__.c.email.type)


def test_people_without_an_address_phone_or_suliko_id_can_coexist() -> None:
    """Uniqueness over nullable columns: NULLs are distinct in PostgreSQL."""
    table = Account.__table__
    assert table.c.email.nullable and table.c.phone.nullable and table.c.suliko_user_id.nullable


def test_downgrade_removes_what_upgrade_added() -> None:
    module, _ = load_revision(FILENAME)
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert {("accounts", "phone"), ("accounts", "suliko_user_id")} <= set(recorder.dropped_columns)
    assert {"uq_accounts_phone", "uq_accounts_suliko_user_id"} <= {
        name for name, _t in recorder.dropped_constraints
    }
    # Deliberately not reverted — see the module docstring.
    assert recorder.altered_columns == []
