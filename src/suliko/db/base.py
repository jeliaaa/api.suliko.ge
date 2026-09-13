"""Declarative base and shared model mixins."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Explicit constraint naming, so Alembic can autogenerate reversible
# migrations. Without this, dropping an unnamed constraint needs hand-written
# SQL because the database picked the name.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """``created_at`` / ``updated_at``, both timezone-aware.

    Defaults are server-side so a row written by a migration or by psql gets
    the same treatment as one written by the ORM.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantScoped:
    """Marks a model as belonging to exactly one tenant.

    Inheriting this is what subjects a model to the automatic filter in
    ``suliko.db.tenancy`` — the ``with_loader_criteria`` predicate keys on this
    class. A tenant-owned table that forgets to inherit it is unprotected, so
    ``tests/test_tenant_isolation.py`` asserts that every table carrying a
    ``tenant_id`` column also inherits this mixin.

    ``tenant_id`` leads every index it appears in: tenant-scoped list queries
    always filter on it first, so a trailing position would leave the index
    unusable for the common case.
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[int]:  # noqa: N805
        return mapped_column(
            ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )


class IdMixin:
    # BIGINT on PostgreSQL; INTEGER on SQLite, because SQLite only gives
    # implicit rowid autoincrement to a column declared exactly INTEGER
    # PRIMARY KEY. The variant keeps production on 64-bit ids while letting
    # the infrastructure-free tests in test_tenant_isolation.py run.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )


def tenant_index(table_name: str, *columns: str, unique: bool = False) -> Index:
    """A composite index led by ``tenant_id``.

    Use for any column a list screen filters or sorts by.
    """
    name = f"{'uq' if unique else 'ix'}_{table_name}_tenant_{'_'.join(columns)}"
    return Index(name, "tenant_id", *columns, unique=unique)


def enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """Persist a Python enum by its VALUE rather than its name.

    SQLAlchemy's ``Enum`` defaults to storing ``.name``, so ``Role.SUPERUSER``
    would land in the database as ``"SUPERUSER"`` and ``CopyType.NOTARY_COPY``
    as ``"NOTARY_COPY"``. The values are the lowercase forms the PHP wrote, the
    API exposes and a human reading the table expects.

    Pass as ``Enum(SomeEnum, values_callable=enum_values, ...)``.
    """
    return [member.value for member in enum_cls]
