"""Migration 0002 must build exactly what the models declare.

Revision 0001 builds from ``Base.metadata``, so it cannot drift. Every later
revision is hand-written, which means it can — and the failure is quiet: the
app starts, the queries compile, and a column is simply missing until someone
touches the one endpoint that uses it.

These tests execute the revision's ``upgrade()`` against a recording stub and
compare the result with the metadata. Nothing here needs a database.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from suliko.models import Base

REVISION = Path(__file__).resolve().parents[1] / "alembic" / "versions"


@dataclass
class RecordedOps:
    """Stands in for ``alembic.op`` and remembers what it was asked to do."""

    tables: dict[str, sa.Table] = field(default_factory=dict)
    indexes: list[tuple[str, str, list[str]]] = field(default_factory=list)
    statements: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    def create_table(self, name: str, *columns: Any, **kwargs: Any) -> sa.Table:
        # A private MetaData: the real one already holds these tables, and
        # re-declaring into it would collide.
        table = sa.Table(name, sa.MetaData(), *columns, **kwargs)
        self.tables[name] = table
        return table

    def create_index(self, name: str, table: str, columns: list[str], **_kwargs: Any) -> None:
        self.indexes.append((name, table, list(columns)))

    def drop_table(self, name: str) -> None:
        self.dropped.append(name)

    def drop_index(self, name: str, **_kwargs: Any) -> None:
        self.dropped.append(name)

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def _load_revision(filename: str) -> tuple[Any, RecordedOps]:
    """Import a revision with ``op`` replaced by the recorder, and run upgrade."""
    path = REVISION / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    recorder = RecordedOps()
    module.op = recorder  # type: ignore[attr-defined]
    module.upgrade()
    return module, recorder


def _import_revision(filename: str) -> Any:
    """Import a revision module without running anything.

    ``_load_revision`` executes ``upgrade()``, which 0001 cannot survive
    against the recorder: it calls ``Base.metadata.create_all(op.get_bind())``,
    and the recorder is not a connection. These tests only need the module's
    declarations.
    """
    path = REVISION / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def revision_0002() -> tuple[Any, RecordedOps]:
    return _load_revision("0002_collaboration_and_cms.py")


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
    first = _import_revision("0001_initial_schema_and_rls.py")
    second = _import_revision("0002_collaboration_and_cms.py")

    third = _import_revision("0003_integration_credentials.py")
    later = set(second.NEW_TABLES) | set(third.NEW_TABLES)

    assert later == first.LATER_REVISION_TABLES, (
        "0001 must exclude every table a later revision creates. Out of sync: "
        f"{later ^ first.LATER_REVISION_TABLES}"
    )

    built_by_0001 = {table.name for table in first._revision_tables()}
    assert not (built_by_0001 & later)


def test_0001_still_builds_the_core_tables() -> None:
    """The exclusion must not go too far the other way."""
    first = _import_revision("0001_initial_schema_and_rls.py")
    built = {table.name for table in first._revision_tables()}

    for core in ("orders", "order_documents", "clients", "users", "tenants", "expenses"):
        assert core in built, f"0001 no longer creates {core}"


def test_0001_applies_rls_to_every_table_it_builds() -> None:
    """A tenant-scoped table with a grant but no policy is readable across
    tenants. The two lists are derived from the same source so they cannot
    drift, and this asserts that they have not."""
    first = _import_revision("0001_initial_schema_and_rls.py")

    scoped = set(first._tenant_scoped_tables())
    built = {t.name for t in first._revision_tables() if "tenant_id" in t.columns}

    assert scoped == built - {"audit_log"}
