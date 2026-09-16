"""A schema one migration behind must say so, not say "internal error".

The frontend deploys on push; this API is a manual pull plus an explicit
`alembic upgrade head`. The two are therefore routinely out of step, and when
they are, every query against a table the newer code expects fails with
SQLSTATE 42P01. Flattened into "An internal error occurred" — which is what the
generic SQLAlchemy handler did — that reads as a bug in the endpoint and gets
debugged as one.

These tests pin the two things that stop that happening: the handler
recognises the condition, and `/health/ready` reports it before a user does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

from suliko.core.errors import _missing_schema_object
from suliko.main import migration_head


class FakePgError(Exception):
    """Stands in for the asyncpg exception SQLAlchemy wraps."""

    def __init__(self, sqlstate: str, message: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def programming_error(sqlstate: str, message: str) -> ProgrammingError:
    return ProgrammingError("SELECT 1", {}, FakePgError(sqlstate, message))


# ── Recognising the condition ───────────────────────────────────────────────


def test_missing_table_is_named() -> None:
    exc = programming_error("42P01", 'relation "notifications" does not exist')
    assert _missing_schema_object(exc) == 'table "notifications"'


def test_missing_column_is_named() -> None:
    exc = programming_error("42703", "column notifications.subject_label does not exist")
    # No quotes in this driver message shape, so it degrades to the kind.
    assert _missing_schema_object(exc) == "a column it expects"


def test_missing_column_with_quoted_identifier() -> None:
    exc = programming_error("42703", 'column "subject_label" does not exist')
    assert _missing_schema_object(exc) == 'column "subject_label"'


@pytest.mark.parametrize("sqlstate", ["42P01", "42703", "42883", "3F000"])
def test_every_schema_sqlstate_is_recognised(sqlstate: str) -> None:
    assert _missing_schema_object(programming_error(sqlstate, "boom")) is not None


# ── NOT mistaking data errors for schema drift ──────────────────────────────


def test_a_constraint_violation_is_not_schema_drift() -> None:
    """23505 is a duplicate key — real data, and its message quotes values.

    Reporting it the way a missing table is reported would leak a row's
    contents to the caller, which is the thing the generic handler exists to
    prevent.
    """
    exc = programming_error("23505", "Key (email)=(nino@example.ge) already exists")
    assert _missing_schema_object(exc) is None


def test_a_connection_failure_is_not_schema_drift() -> None:
    exc = OperationalError("SELECT 1", {}, Exception("connection refused"))
    assert _missing_schema_object(exc) is None


def test_an_exception_with_no_sqlstate_is_not_schema_drift() -> None:
    exc = ProgrammingError("SELECT 1", {}, Exception("something else"))
    assert _missing_schema_object(exc) is None


# ── The readiness check knows what it expects ───────────────────────────────


def test_migration_head_is_the_one_nothing_supersedes() -> None:
    """The readiness check must name the newest revision without being told.

    Computed here a second way — from the filenames — so that adding a
    revision cannot leave `/health/ready` comparing against a stale head and
    reporting a correctly-migrated database as pending.
    """
    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"

    revisions: set[str] = set()
    parents: set[str] = set()
    for path in versions.glob("[0-9]*.py"):
        text = path.read_text(encoding="utf-8")
        revisions.update(re.findall(r'^revision:\s*str\s*=\s*"([^"]+)"', text, re.M))
        parents.update(re.findall(r'^down_revision:[^=]*=\s*"([^"]+)"', text, re.M))

    heads = revisions - parents
    assert len(heads) == 1, f"migration history has {len(heads)} heads: {heads}"
    assert migration_head() == heads.pop()


def test_every_revision_is_reachable_from_the_first() -> None:
    """No orphans: a revision whose parent does not exist is never applied,
    and Alembic reports it as a separate head rather than an error."""
    versions = Path(__file__).resolve().parents[1] / "alembic" / "versions"

    parents: dict[str, str | None] = {}
    for path in versions.glob("[0-9]*.py"):
        text = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision:\s*str\s*=\s*"([^"]+)"', text, re.M)
        down = re.search(r'^down_revision:[^=]*=\s*"([^"]+)"', text, re.M)
        assert revision
        parents[revision.group(1)] = down.group(1) if down else None

    roots = [r for r, p in parents.items() if p is None]
    assert len(roots) == 1, f"expected exactly one root revision, found {roots}"

    for revision, parent in parents.items():
        assert parent is None or parent in parents, (
            f"revision {revision} names a parent that does not exist: {parent}"
        )
