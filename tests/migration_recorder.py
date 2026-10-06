"""Run a migration revision against a recorder instead of a database.

Shared by the migration parity tests. Not a test module itself (no ``test_``
prefix), so pytest does not collect it.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa

REVISIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


@dataclass
class RecordedOps:
    """Stands in for ``alembic.op`` and remembers what it was asked to do."""

    tables: dict[str, sa.Table] = field(default_factory=dict)
    indexes: list[tuple[str, str, list[str]]] = field(default_factory=list)
    #: Keyword options each index was created with (``unique``, ...), by name.
    index_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    statements: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    #: Columns added to tables an EARLIER revision owns, as (table, column).
    added_columns: list[tuple[str, sa.Column[object]]] = field(default_factory=list)
    dropped_columns: list[tuple[str, str]] = field(default_factory=list)
    #: (table, column, nullable) for each `alter_column` that changed nullability.
    altered_columns: list[tuple[str, str, bool]] = field(default_factory=list)
    #: (name, table, columns) for each unique constraint created on an existing table.
    unique_constraints: list[tuple[str, str, list[str]]] = field(default_factory=list)
    dropped_constraints: list[tuple[str, str]] = field(default_factory=list)

    def get_bind(self) -> None:
        """There is no database.

        Revisions that inspect the schema before acting — the guarded
        `add_column` in 0006 — branch on this. Returning None makes them take
        the "nothing exists yet" path, which is what records the operation so
        a test can see it.
        """
        return None

    def add_column(self, table: str, column: sa.Column[object], **_kwargs: Any) -> None:
        self.added_columns.append((table, column))

    def drop_column(self, table: str, name: str, **_kwargs: Any) -> None:
        self.dropped_columns.append((table, name))

    def alter_column(
        self, table: str, column: str, *, nullable: bool | None = None, **_kwargs: Any
    ) -> None:
        if nullable is not None:
            self.altered_columns.append((table, column, nullable))

    def create_unique_constraint(self, name: str, table: str, columns: list[str]) -> None:
        self.unique_constraints.append((name, table, list(columns)))

    def drop_constraint(self, name: str, table: str, **_kwargs: Any) -> None:
        self.dropped_constraints.append((name, table))

    def create_table(self, name: str, *columns: Any, **kwargs: Any) -> sa.Table:
        # A private MetaData: the real one already holds these tables, and
        # re-declaring into it would collide.
        table = sa.Table(name, sa.MetaData(), *columns, **kwargs)
        self.tables[name] = table
        return table

    def create_index(self, name: str, table: str, columns: list[str], **kwargs: Any) -> None:
        self.indexes.append((name, table, list(columns)))
        self.index_options[name] = dict(kwargs)

    def drop_table(self, name: str) -> None:
        self.dropped.append(name)

    def drop_index(self, name: str, **_kwargs: Any) -> None:
        self.dropped.append(name)

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))


def import_revision(filename: str) -> Any:
    path = REVISIONS / filename
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_revision(filename: str) -> tuple[Any, RecordedOps]:
    """Import a revision with ``op`` replaced by the recorder, and run upgrade."""
    module = import_revision(filename)
    recorder = RecordedOps()
    module.op = recorder
    module.upgrade()
    return module, recorder
