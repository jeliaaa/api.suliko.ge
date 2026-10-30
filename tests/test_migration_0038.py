"""Revision 0038: Claude Translate's queue, the worker heartbeat and two columns."""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.db.base import TenantScoped
from suliko.models import Base, ClaudeTranslationJob, ClaudeWorkerHeartbeat

JOBS = "claude_translation_jobs"
HEARTBEATS = "claude_worker_heartbeats"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision("0038_claude_translate.py")


def test_creates_the_two_tables_and_adds_the_two_columns(
    revision: tuple[Any, RecordedOps],
) -> None:
    module, ops = revision
    assert set(ops.tables) == {JOBS, HEARTBEATS} == set(module.NEW_TABLES)
    assert {(t, c.name) for t, c in ops.added_columns} == {
        ("tenants", "claude_translate_enabled"),
        ("orders", "translation_instructions"),
    }


@pytest.mark.parametrize("table", [JOBS, HEARTBEATS])
def test_columns_match_the_model(revision: tuple[Any, RecordedOps], table: str) -> None:
    _, ops = revision
    migrated = ops.tables[table]
    declared = Base.metadata.tables[table]
    assert set(migrated.columns.keys()) == set(declared.columns.keys())
    for name, column in declared.columns.items():
        other = migrated.columns[name]
        assert other.nullable == column.nullable, f"{name}: nullability differs"
        assert str(other.type) == str(column.type), f"{name}: type differs"


def test_added_columns_match_the_models(revision: tuple[Any, RecordedOps]) -> None:
    _, ops = revision
    for table, column in ops.added_columns:
        declared = Base.metadata.tables[table].columns[column.name]
        assert str(column.type) == str(declared.type)
        assert column.nullable == declared.nullable
    enabled = next(c for _, c in ops.added_columns if c.name == "claude_translate_enabled")
    assert enabled.server_default is not None  # every existing organisation starts off


def test_the_queue_is_tenant_data_and_the_heartbeat_is_not(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    assert issubclass(ClaudeTranslationJob, TenantScoped)
    assert not issubclass(ClaudeWorkerHeartbeat, TenantScoped)
    assert "tenant_id" not in ops.tables[HEARTBEATS].columns
    sql = "\n".join(ops.statements)
    assert f"ALTER TABLE {JOBS} FORCE ROW LEVEL SECURITY" in sql
    assert f"CREATE POLICY tenant_isolation ON {JOBS}" in sql
    assert f"ON {HEARTBEATS} TO suliko_app" in sql
    assert f"ON {HEARTBEATS}\n" not in sql  # no policy on the platform table


def test_one_active_job_per_file_is_a_partial_unique_index(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    name = "uq_claude_translation_jobs_active_file"
    assert (name, JOBS, ["source_file_id"]) in ops.indexes
    assert ops.index_options[name]["unique"] is True
    assert "queued" in str(ops.index_options[name]["postgresql_where"])


def test_downgrade_drops_what_upgrade_made(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert set(recorder.dropped) == {JOBS, HEARTBEATS}
    assert set(recorder.dropped_columns) == {
        ("tenants", "claude_translate_enabled"),
        ("orders", "translation_instructions"),
    }
