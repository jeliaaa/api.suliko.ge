"""Revision 0017: the record of each Suliko Translate job started from an order.

One new tenant table, which must match its model, be isolated like every other
tenant table, and keep its rows when the things they point at are removed.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from migration_recorder import RecordedOps, load_revision
from suliko.db.base import TenantScoped
from suliko.models import Base

REVISION = "0017_document_translations.py"
TABLE = "document_translations"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(REVISION)


def test_creates_the_one_table(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert set(ops.tables) == {TABLE} == set(module.NEW_TABLES)
    assert ops.dropped == []


def test_columns_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = ops.tables[TABLE]
    declared = Base.metadata.tables[TABLE]

    assert set(migrated.columns.keys()) == set(declared.columns.keys())
    for name, column in declared.columns.items():
        other = migrated.columns[name]
        assert other.nullable == column.nullable, f"{TABLE}.{name}: nullability differs"
        assert str(other.type) == str(column.type), f"{TABLE}.{name}: type differs"


def test_indexes_and_uniques_match_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    declared = Base.metadata.tables[TABLE]
    migrated = {name for name, table, _ in ops.indexes if table == TABLE}
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

    assert uniques(ops.tables[TABLE]) == uniques(declared)


def test_the_status_check_names_every_status(revision: tuple[Any, RecordedOps]) -> None:
    from suliko.models.translation import TranslationStatus

    _, ops = revision
    (check,) = [c for c in ops.tables[TABLE].constraints if isinstance(c, sa.CheckConstraint)]
    assert check.name == "ck_document_translations_translation_status"
    for status in TranslationStatus:
        assert f"'{status.value}'" in str(check.sqltext)


def test_a_removed_document_or_file_leaves_the_record(revision: tuple[Any, RecordedOps]) -> None:
    """The pages were spent whatever happened to the file afterwards."""
    _, ops = revision
    on_delete = {
        fk.parent.name: fk.ondelete
        for fk in ops.tables[TABLE].foreign_keys
        if fk.parent.name != "tenant_id"
    }
    assert on_delete == {
        "order_document_id": "SET NULL",
        "source_file_id": "SET NULL",
        "result_file_id": "SET NULL",
        "requested_by_user_id": "SET NULL",
    }


def test_it_is_isolated_per_tenant(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    sql = "\n".join(ops.statements)
    assert f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY" in sql
    assert f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY" in sql
    assert f"CREATE POLICY tenant_isolation ON {TABLE}" in sql
    assert f"ON {TABLE} TO suliko_app" in sql

    by_table = {mapper.local_table.name: mapper.class_ for mapper in Base.registry.mappers}
    assert issubclass(by_table[TABLE], TenantScoped)


def test_downgrade_drops_it(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert recorder.dropped == [TABLE]
