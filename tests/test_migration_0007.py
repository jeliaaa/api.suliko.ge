"""Migration 0007 must build exactly what the invite model declares.

Same approach as ``test_migration_0004.py``, whose docstring this one
deliberately re-explains: no database, everything runs against a recorder,
and the check that matters most is that ``portal_account_invites`` is granted
to the app role and gets NO row-level security policy — the whole point of
this table is being searchable across every bureau before any tenant is
known.
"""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.db.base import TenantScoped
from suliko.models import Base

FILENAME = "0007_portal_account_invites.py"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(FILENAME)


def test_creates_exactly_the_listed_tables(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert set(ops.tables) == set(module.NEW_TABLES)
    assert not set(module.PLATFORM_TABLES) & set(module.TENANT_TABLES)


def _table_names() -> list[str]:
    module, _ = load_revision(FILENAME)
    return list(module.NEW_TABLES)


@pytest.mark.parametrize("table_name", _table_names())
def test_columns_match_the_model(revision: tuple[Any, RecordedOps], table_name: str) -> None:
    _, ops = revision
    migrated = ops.tables[table_name]
    declared = Base.metadata.tables[table_name]

    assert set(migrated.columns.keys()) == set(declared.columns.keys()), (
        f"{table_name}: column names differ"
    )
    for name, column in declared.columns.items():
        other = migrated.columns[name]
        assert other.nullable == column.nullable, f"{table_name}.{name}: nullability differs"
        assert str(other.type) == str(column.type), f"{table_name}.{name}: type differs"


@pytest.mark.parametrize("table_name", _table_names())
def test_indexes_match_the_model(revision: tuple[Any, RecordedOps], table_name: str) -> None:
    _, ops = revision
    migrated = {name for name, table, _ in ops.indexes if table == table_name}
    declared = {index.name for index in Base.metadata.tables[table_name].indexes}
    assert migrated == declared, f"{table_name}: index set differs"


@pytest.mark.parametrize("table_name", _table_names())
def test_unique_constraints_match_the_model(
    revision: tuple[Any, RecordedOps], table_name: str
) -> None:
    """The two unique constraints ARE the access rules: one invite per address
    per bureau, and one invite per directory row — mirroring the guarantees
    ``portal_translator_links`` already makes for a resolved link."""
    import sqlalchemy as sa

    _, ops = revision

    def uniques(table: sa.Table) -> set[str | None]:
        return {
            str(c.name)
            for c in table.constraints
            if isinstance(c, sa.UniqueConstraint) and c.name is not None
        }

    assert uniques(ops.tables[table_name]) == uniques(Base.metadata.tables[table_name])


def test_tenant_tables_get_row_level_security(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    sql = "\n".join(ops.statements)
    for table in module.TENANT_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
        assert f"CREATE POLICY tenant_isolation ON {table}" in sql


def test_platform_tables_get_no_policy(revision: tuple[Any, RecordedOps]) -> None:
    """A tenant_isolation policy here would make the table unreadable before a
    tenant is bound — which is exactly when resolution needs to search it."""
    module, ops = revision
    sql = "\n".join(ops.statements)
    for table in module.PLATFORM_TABLES:
        assert f"CREATE POLICY tenant_isolation ON {table}\n" not in sql
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" not in sql


def test_every_new_table_is_granted_to_the_app_role(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    sql = "\n".join(ops.statements)
    for table in module.NEW_TABLES:
        assert f"ON {table} TO suliko_app" in sql


def test_groups_agree_with_the_models(revision: tuple[Any, RecordedOps]) -> None:
    """A tenant table is a TenantScoped model; a platform table is not."""
    module, _ = revision
    by_table = {mapper.local_table.name: mapper.class_ for mapper in Base.registry.mappers}
    for table in module.TENANT_TABLES:
        assert issubclass(by_table[table], TenantScoped), f"{table} should be TenantScoped"
    for table in module.PLATFORM_TABLES:
        assert not issubclass(by_table[table], TenantScoped), f"{table} should be platform-level"


def test_downgrade_drops_everything_upgrade_created(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert set(recorder.dropped) == set(module.NEW_TABLES)
