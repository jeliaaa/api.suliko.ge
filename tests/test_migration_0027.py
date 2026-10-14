"""Revision 0027: the reverse of every stored translator pair. Data only."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0027_translator_pairs_both_ways.py")


def test_changes_no_schema(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    assert not ops.tables
    assert not ops.added_columns
    assert not ops.dropped_columns


def test_runs_per_tenant_under_row_level_security(revision: tuple[Any, RecordedOps]) -> None:
    """FORCE ROW LEVEL SECURITY binds the owner too; the backfill sets the tenant."""
    _, ops = revision
    [statement] = [s for s in ops.statements if "translator_language_pairs" in s]
    assert "set_config('suliko.tenant_id', t.id::text, true)" in statement
    assert "set_config('suliko.tenant_id', '', true)" in statement


def test_adds_the_reverse_only_where_it_is_missing(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    [statement] = [s for s in ops.statements if "translator_language_pairs" in s]
    # The new row swaps the two languages and carries no rate of its own.
    assert "p.target_language, p.source_language, NULL" in statement
    # A reverse that exists already (with its own rate) is left alone.
    assert "NOT EXISTS" in statement
    assert "r.source_language = p.target_language" in statement
    assert "r.target_language = p.source_language" in statement


def test_downgrade_has_nothing_to_undo(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    before = len(ops.statements)
    module.downgrade()
    assert len(ops.statements) == before
