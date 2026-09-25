"""Migration 0002 must build exactly what the models declare.

Revision 0001 builds from ``Base.metadata``. Every later revision is
hand-written, which means it can drift — and the failure is quiet: the app
starts, the queries compile, and a column is simply missing until someone
touches the one endpoint that uses it.

These tests execute the revision's ``upgrade()`` against a recording stub and
compare the result with the metadata. Nothing here needs a database. Revision
0004 has its own module, ``test_migration_0004.py``; the recording stub lives in
``migration_recorder.py`` so both can use it.
"""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, import_revision, load_revision
from suliko.models import Base

#: Every revision after 0001, in order. Each creates its own ``NEW_TABLES``.
LATER_REVISIONS = (
    "0002_collaboration_and_cms.py",
    "0003_integration_credentials.py",
    "0004_translator_portal_and_drive.py",
    # Data only — no tables, hence an empty NEW_TABLES. Listed so the
    # single-head and exclusion-list checks still see it.
    "0005_tenant_plans.py",
    "0006_user_invites_and_permission_overrides.py",
    "0007_portal_account_invites.py",
    # Columns and indexes only — see test_migration_0008.py.
    "0008_tenant_timezone_and_money_guards.py",
)


@pytest.fixture(scope="module")
def revision_0002() -> tuple[Any, RecordedOps]:
    return load_revision("0002_collaboration_and_cms.py")


def test_creates_exactly_the_new_tables(revision_0002: tuple[Any, RecordedOps]) -> None:
    module, ops = revision_0002
    assert set(ops.tables) == set(module.NEW_TABLES)


@pytest.mark.parametrize(
    "table_name",
    [
        "order_comments",
        "order_comment_mentions",
        "order_comment_reads",
        "notifications",
        "service_pages",
        "site_strings",
    ],
)
def test_columns_match_the_model(revision_0002: tuple[Any, RecordedOps], table_name: str) -> None:
    """Names, types and nullability, column by column."""
    _, ops = revision_0002
    migrated = ops.tables[table_name]
    declared = Base.metadata.tables[table_name]

    assert set(migrated.columns.keys()) == set(declared.columns.keys()), (
        f"{table_name}: column names differ"
    )

    for name, column in declared.columns.items():
        other = migrated.columns[name]
        assert other.nullable == column.nullable, f"{table_name}.{name}: nullability differs"
        # Compared as compiled DDL types: `String(30)` and `Enum(..., length=30,
        # native_enum=False)` are different Python objects but the same column.
        assert str(other.type) == str(column.type), f"{table_name}.{name}: type differs"


@pytest.mark.parametrize(
    "table_name",
    [
        "order_comments",
        "order_comment_mentions",
        "order_comment_reads",
        "notifications",
        "service_pages",
        "site_strings",
    ],
)
def test_indexes_match_the_model(revision_0002: tuple[Any, RecordedOps], table_name: str) -> None:
    """A missing index is a silent performance cliff, not an error."""
    _, ops = revision_0002
    migrated = {name for name, table, _ in ops.indexes if table == table_name}
    declared = {index.name for index in Base.metadata.tables[table_name].indexes}
    assert migrated == declared, f"{table_name}: index set differs"


def test_every_new_table_gets_row_level_security(
    revision_0002: tuple[Any, RecordedOps],
) -> None:
    """The failure this exists to catch: a table granted to the app role but
    left without a policy is readable across every tenant."""
    module, ops = revision_0002
    sql = "\n".join(ops.statements)

    for table in module.NEW_TABLES:
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql, (
            f"{table} does not enable RLS"
        )
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql, (
            f"{table} does not FORCE RLS — the table owner would bypass it"
        )
        assert f"CREATE POLICY tenant_isolation ON {table}" in sql, (
            f"{table} has no tenant_isolation policy"
        )
        assert f"ON {table} TO suliko_app" in sql, (
            f"{table} is not granted to the app role — 0001's ALL TABLES grant "
            f"was a snapshot and does not cover it"
        )


def test_every_new_table_is_tenant_scoped(revision_0002: tuple[Any, RecordedOps]) -> None:
    """RLS keys on `tenant_id`; a table without one cannot be isolated."""
    _, ops = revision_0002
    for name, table in ops.tables.items():
        assert "tenant_id" in table.columns, f"{name} has no tenant_id"


def test_downgrade_drops_everything_upgrade_created(
    revision_0002: tuple[Any, RecordedOps],
) -> None:
    module, _ = revision_0002

    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()

    assert set(recorder.dropped) == set(module.NEW_TABLES)


# ── The two revisions must not both create the same table ───────────────────


def test_0001_does_not_build_tables_that_0002_owns() -> None:
    """The bug this pins: 0001 builds from ``Base.metadata``, which is the
    CURRENT models package, not the models as they stood when 0001 was
    written. Left unscoped, it creates the six collaboration and CMS tables
    and 0002 then dies trying to create them again — so a fresh database can
    never reach head.
    """
    first = import_revision("0001_initial_schema_and_rls.py")
    later = set().union(*(import_revision(name).NEW_TABLES for name in LATER_REVISIONS))

    assert later == first.LATER_REVISION_TABLES, (
        "0001 must exclude every table a later revision creates. Out of sync: "
        f"{later ^ first.LATER_REVISION_TABLES}"
    )

    built_by_0001 = {table.name for table in first._revision_tables()}
    assert not (built_by_0001 & later)


def test_every_model_table_is_created_by_exactly_one_revision() -> None:
    """The same failure between later revisions: a table two of them create
    fails the second on a fresh database, and a table none of them creates
    exists only in tests."""
    seen = {
        table.name for table in import_revision("0001_initial_schema_and_rls.py")._revision_tables()
    }
    for name in LATER_REVISIONS:
        tables = set(import_revision(name).NEW_TABLES)
        overlap = seen & tables
        assert not overlap, f"{name} creates tables an earlier revision already creates: {overlap}"
        seen |= tables

    assert seen == set(Base.metadata.tables), (
        f"models without a migration: {sorted(set(Base.metadata.tables) - seen)} | "
        f"migrated but not modelled: {sorted(seen - set(Base.metadata.tables))}"
    )


def test_0001_still_builds_the_core_tables() -> None:
    """The exclusion must not go too far the other way."""
    first = import_revision("0001_initial_schema_and_rls.py")
    built = {table.name for table in first._revision_tables()}

    for core in ("orders", "order_documents", "clients", "users", "tenants", "expenses"):
        assert core in built, f"0001 no longer creates {core}"


def test_0001_applies_rls_to_every_table_it_builds() -> None:
    """A tenant-scoped table with a grant but no policy is readable across
    tenants. The two lists are derived from the same source so they cannot
    drift, and this asserts that they have not."""
    first = import_revision("0001_initial_schema_and_rls.py")

    scoped = set(first._tenant_scoped_tables())
    built = {t.name for t in first._revision_tables() if "tenant_id" in t.columns}

    assert scoped == built - {"audit_log"}
