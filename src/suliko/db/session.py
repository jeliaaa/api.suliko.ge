"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from suliko.config import get_settings
from suliko.db.tenancy import try_get_current_tenant_id

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            str(settings.database_url),
            echo=settings.db_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            # asyncpg caches prepared statements per connection; with a pooler
            # like PgBouncer in transaction mode that cache goes stale and
            # errors. Disabling costs a little latency and removes a whole
            # class of production-only failure.
            connect_args={"statement_cache_size": 0},
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """A transactional session with the tenant GUC applied.

    The GUC (``suliko.tenant_id``) is what the PostgreSQL row-level security
    policies read. It is set with ``set_config(..., true)`` — the ``true``
    makes it *transaction-local*, so it cannot leak to the next request that
    borrows this pooled connection. That detail is the difference between RLS
    working and RLS being a decoration.
    """
    async with get_sessionmaker()() as session:
        tenant_id = try_get_current_tenant_id()
        if tenant_id is not None:
            await session.execute(
                text("SELECT set_config('suliko.tenant_id', :tid, true)"),
                {"tid": str(tenant_id)},
            )
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def bind_tenant_guc(session: AsyncSession, tenant_id: int) -> None:
    """Apply the RLS GUC to a session that was opened before the tenant was known.

    ``session_scope`` sets this at open time from the ambient context, which is
    right for an authenticated request — ``get_current_session`` has already
    resolved the tenant by then. The auth endpoints are the exception: they
    open a session in order to *find* the user, and only then learn which
    tenant they are acting for. Without this, every tenant-scoped row they
    write (a session, a reset token) is inserted with no GUC set, and the
    ``WITH CHECK`` on the ``tenant_isolation`` policy rejects it.

    Transaction-local, exactly as in ``session_scope`` — it must not leak to
    the next request that borrows this pooled connection.

    A no-op on anything that is not PostgreSQL, so the SQLite-backed tests can
    exercise the same code paths.
    """
    if session.bind is None or session.bind.dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT set_config('suliko.tenant_id', :tid, true)"),
        {"tid": str(tenant_id)},
    )


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency.

    Ordering matters: the session must be created *after* the tenant context
    is set, or the GUC above is written with no tenant. ``get_current_session``
    sets the context and is depended on first — see ``suliko.api.deps``.
    """
    async with session_scope() as session:
        yield session
