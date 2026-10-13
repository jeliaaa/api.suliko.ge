"""Revision 0022: oral interpreting lines.

Two columns that must match the models, and one NOT NULL relaxed. No database:
the revision runs against the recorder.
"""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base

FILENAME = "0022_oral_interpreting.py"
COLUMNS = {("order_documents", "service_type"), ("language_pair_prices", "price_per_minute")}


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(FILENAME)


def test_adds_the_two_columns_only(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    assert {(t, c.name) for t, c in ops.added_columns} == COLUMNS
    assert not ops.tables


@pytest.mark.parametrize("key", sorted(COLUMNS))
def test_each_column_matches_the_model(
    revision: tuple[Any, RecordedOps], key: tuple[str, str]
) -> None:
    _, ops = revision
    migrated = next(c for t, c in ops.added_columns if (t, c.name) == key)
    declared = Base.metadata.tables[key[0]].columns[key[1]]
    assert str(migrated.type) == str(declared.type)
    assert migrated.nullable is declared.nullable


def test_existing_lines_are_written(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    column = next(c for _, c in ops.added_columns if c.name == "service_type")
    assert str(column.server_default.arg) == "written"


def test_the_document_type_becomes_optional_and_the_model_agrees(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    assert ("order_documents", "document_type_id", True) in ops.altered_columns
    assert Base.metadata.tables["order_documents"].columns["document_type_id"].nullable is True


def test_downgrade_removes_oral_lines_first(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert any(
        "DELETE FROM order_documents WHERE service_type = 'oral'" in s for s in recorder.statements
    )
    assert ("order_documents", "document_type_id", False) in recorder.altered_columns
    assert set(recorder.dropped_columns) >= COLUMNS
