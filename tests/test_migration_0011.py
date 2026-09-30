"""Migration 0011 — accounts, and the backfill that merges per-organisation logins.

No database: the revision runs against the recorder, where every table and
column looks missing, so each guarded operation is recorded and compared with
the models. The backfill itself is PostgreSQL and is pinned by its SQL.
"""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, import_revision, load_revision
from suliko.models import Account, Base, Tenant, User

FILENAME = "0011_accounts.py"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(FILENAME)


def test_accounts_matches_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = ops.tables["accounts"]
    declared = Account.__table__
    assert set(migrated.columns.keys()) == set(declared.columns.keys())
    for name, column in declared.columns.items():
        other = migrated.columns[name]
        assert other.nullable == column.nullable, f"accounts.{name}: nullability differs"
        assert str(other.type) == str(column.type), f"accounts.{name}: type differs"


def test_fresh_databases_get_accounts_from_0001() -> None:
    """`users.account_id` points at accounts, and 0001 builds users from the
    models — so 0001 must build accounts too, or a fresh database fails."""
    first = import_revision("0001_initial_schema_and_rls.py")
    assert "accounts" not in first.LATER_REVISION_TABLES
    assert "accounts" in {table.name for table in first._revision_tables()}


@pytest.mark.parametrize(
    ("table", "column"),
    [("users", "account_id"), ("users", "invitation_pending"), ("tenants", "is_personal")],
)
def test_added_columns_match_the_models(
    revision: tuple[Any, RecordedOps], table: str, column: str
) -> None:
    _, ops = revision
    added = {(t, c.name): c for t, c in ops.added_columns}
    migrated = added[(table, column)]
    declared = {"users": User, "tenants": Tenant}[table].__table__.c[column]
    assert migrated.nullable == declared.nullable
    assert str(migrated.type) == str(declared.type)
    assert (migrated.server_default is None) == (declared.server_default is None)


def test_the_account_link_is_indexed(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    assert ("ix_users_account_id", "users", ["account_id"]) in ops.indexes
    assert "ix_users_account_id" in {i.name for i in Base.metadata.tables["users"].indexes}


def test_the_backfill_keeps_the_most_recently_used_password(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    sql = "\n".join(ops.statements)
    assert "ORDER BY lower(u.email), u.last_login_at DESC NULLS LAST" in sql
    assert "ON CONFLICT (email) DO UPDATE" in sql
    assert "EXCLUDED.last_login_at > accounts.last_login_at" in sql


def test_the_backfill_runs_inside_each_tenants_scope(revision: tuple[Any, RecordedOps]) -> None:
    """FORCE RLS on users would hide every row from a non-superuser migration."""
    _, ops = revision
    sql = "\n".join(ops.statements)
    assert "set_config('suliko.tenant_id', t.id::text, true)" in sql
    assert "u.tenant_id = t.id" in sql


def test_accounts_is_granted_and_not_tenant_isolated(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    sql = "\n".join(ops.statements)
    assert "ON accounts TO suliko_app" in sql
    assert "POLICY tenant_isolation ON accounts" not in sql
    assert "tenant_id" not in Account.__table__.columns


def test_downgrade_removes_what_upgrade_added() -> None:
    module, _ = load_revision(FILENAME)
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert "accounts" in recorder.dropped
    assert {
        ("users", "account_id"),
        ("users", "invitation_pending"),
        ("tenants", "is_personal"),
    } <= set(recorder.dropped_columns)
