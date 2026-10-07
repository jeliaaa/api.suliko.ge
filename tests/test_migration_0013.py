"""Revision 0013: order files move into Suliko's own storage.

One new tenant table that must match its model and be isolated like every other
tenant table, and three Drive tables that go — with a downgrade that brings
them back.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from migration_recorder import RecordedOps, import_revision, load_revision
from suliko.db.base import TenantScoped
from suliko.models import Base

REVISION = "0013_order_file_storage.py"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(REVISION)


def test_creates_order_files_and_drops_the_drive_tables(
    revision: tuple[Any, RecordedOps],
) -> None:
    module, ops = revision
    assert set(ops.tables) == {"order_files"} == set(module.NEW_TABLES)
    assert (
        set(ops.dropped)
        == set(module.DROPPED_TABLES)
        == {
            "drive_settings",
            "order_drive_folders",
            "order_document_drive_folders",
        }
    )


def test_the_drive_tables_were_the_ones_0004_created() -> None:
    created = set(import_revision("0004_translator_portal_and_drive.py").TENANT_TABLES)
    assert set(import_revision(REVISION).DROPPED_TABLES) == created


def test_columns_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = ops.tables["order_files"]
    declared = Base.metadata.tables["order_files"]

    assert set(migrated.columns.keys()) == set(declared.columns.keys())
    for name, column in declared.columns.items():
        other = migrated.columns[name]
        assert other.nullable == column.nullable, f"order_files.{name}: nullability differs"
        assert str(other.type) == str(column.type), f"order_files.{name}: type differs"


def test_indexes_and_uniques_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    declared = Base.metadata.tables["order_files"]
    migrated = {name for name, table, _ in ops.indexes if table == "order_files"}
    assert migrated == {index.name for index in declared.indexes}

    def uniques(table: sa.Table) -> set[str]:
        names = {
            str(c.name)
            for c in table.constraints
            if isinstance(c, sa.UniqueConstraint) and c.name is not None
        }
        # A `unique=True` column surfaces as a constraint on the migrated side
        # and as a column flag on the model side.
        names |= {f"uq_{table.name}_{c.name}" for c in table.columns if c.unique}
        return names

    assert uniques(ops.tables["order_files"]) == uniques(declared)


def test_a_documents_deletion_leaves_its_files_for_the_purge(
    revision: tuple[Any, RecordedOps],
) -> None:
    """CASCADE would delete the rows and orphan the bytes in the bucket."""
    _, ops = revision
    (fk,) = [
        fk
        for fk in ops.tables["order_files"].foreign_keys
        if fk.target_fullname == "order_documents.id"
    ]
    assert fk.ondelete == "SET NULL"


def test_order_files_is_isolated_per_tenant(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    sql = "\n".join(ops.statements)
    assert "ALTER TABLE order_files ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE order_files FORCE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY tenant_isolation ON order_files" in sql
    assert "ON order_files TO suliko_app" in sql

    by_table = {mapper.local_table.name: mapper.class_ for mapper in Base.registry.mappers}
    assert issubclass(by_table["order_files"], TenantScoped)


def test_downgrade_restores_the_drive_tables(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()

    assert recorder.dropped == ["order_files"]
    assert set(recorder.tables) == set(module.DROPPED_TABLES)
    sql = "\n".join(recorder.statements)
    for table in module.DROPPED_TABLES:
        assert f"CREATE POLICY tenant_isolation ON {table}" in sql
