"""Revision 0026: the editable text of a translation, one nullable column."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import Base


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0026_translation_html.py")


def test_adds_only_the_html_column(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    assert {(t, c.name) for t, c in ops.added_columns} == {("document_translations", "html")}
    assert not ops.tables


def test_matches_the_model(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    migrated = next(c for _, c in ops.added_columns)
    declared = Base.metadata.tables["document_translations"].columns["html"]
    assert str(migrated.type) == str(declared.type)
    assert migrated.nullable is declared.nullable is True


def test_downgrade_drops_it(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    module.downgrade()
    assert ("document_translations", "html") in set(ops.dropped_columns)
