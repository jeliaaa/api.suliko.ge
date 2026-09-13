"""Tenant isolation.

This module is the load-bearing security control of the whole product. If it
is wrong, one partner bureau can read another's clients, orders and bank
details. Read docs/03-SECURITY-AND-TENANCY.md §2 before changing anything here.

## The rule

``tenant_id`` is resolved from the authenticated session and NEVER from user
input — not a header, not a query string, not a body field, not a subdomain.
Any code path that accepts a caller-supplied tenant id is a horizontal
privilege-escalation bug.

## Three layers, deliberately redundant

1. **The ambient tenant context** (this file). A ``ContextVar`` set once per
   request from the session. Because it is a ContextVar it is correct under
   asyncio concurrency — a plain module global would leak across interleaved
   requests, which is exactly the bug class we cannot afford.

2. **An automatic SQLAlchemy filter** (this file). Every ORM query against a
   ``TenantScoped`` model gets ``WHERE tenant_id = :current`` appended by an
   ORM event, and every INSERT gets ``tenant_id`` stamped. Feature code cannot
   forget, because feature code never writes the clause.

3. **PostgreSQL row-level security** (in the migration). The application's
   database role is subject to RLS policies keyed on
   ``current_setting('suliko.tenant_id')``. This catches raw SQL, a mistaken
   ``session.execute(text(...))``, and anything that bypasses layer 2.

Layer 2 without layer 3 is one forgotten ``.execute(text(...))`` away from a
leak. Layer 3 without layer 2 produces confusing empty results instead of
clear errors. Keep both.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

# ── Ambient tenant context ──────────────────────────────────────────────────

_current_tenant_id: ContextVar[int | None] = ContextVar("current_tenant_id", default=None)

# Set only where crossing tenants is legitimate and audited: the platform
# superuser area, background jobs iterating tenants, and Alembic.
_bypass_tenant_filter: ContextVar[bool] = ContextVar("bypass_tenant_filter", default=False)


class TenantContextError(RuntimeError):
    """Raised when tenant-scoped data is touched with no tenant in context.

    This is deliberately an error rather than an empty result set. A query
    that silently returns nothing looks like "no data" and gets debugged for
    an hour; an exception points at the missing dependency immediately.
    """


def get_current_tenant_id() -> int:
    tenant_id = _current_tenant_id.get()
    if tenant_id is None:
        raise TenantContextError(
            "No tenant in context. Tenant-scoped queries require a request "
            "authenticated through get_current_session, or an explicit "
            "bypass_tenant_scope() block."
        )
    return tenant_id


def try_get_current_tenant_id() -> int | None:
    """The tenant id, or None. For logging and diagnostics only."""
    return _current_tenant_id.get()


def set_current_tenant_id(tenant_id: int | None) -> Any:
    """Set the ambient tenant. Returns a token for ``reset``."""
    return _current_tenant_id.set(tenant_id)


def reset_current_tenant_id(token: Any) -> None:
    _current_tenant_id.reset(token)


@contextmanager
def tenant_scope(tenant_id: int) -> Iterator[None]:
    """Run a block scoped to one tenant."""
    token = _current_tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _current_tenant_id.reset(token)


@contextmanager
def bypass_tenant_scope() -> Iterator[None]:
    """Disable the automatic tenant filter for this block.

    Only for: platform superuser operations, cross-tenant background jobs, and
    migrations. Every use in application code should be accompanied by an
    audit-log entry, and should be obvious in review — if you are reaching for
    this in a feature handler, the design is wrong.
    """
    token = _bypass_tenant_filter.set(True)
    try:
        yield
    finally:
        _bypass_tenant_filter.reset(token)


def is_bypassed() -> bool:
    return _bypass_tenant_filter.get()


# ── Automatic query filtering ───────────────────────────────────────────────


def install_tenant_filter() -> None:
    """Register the ORM event that scopes every tenant-bound query.

    Called once from the app factory. Uses ``with_loader_criteria`` so the
    predicate applies to eagerly-loaded relationships too, not just the root
    entity — a subtlety that a naive ``query.filter()`` wrapper misses, and
    the reason relationship loads cannot be used to escape the filter.
    """
    from suliko.db.base import TenantScoped

    @event.listens_for(Session, "do_orm_execute")
    def _apply_tenant_filter(state: ORMExecuteState) -> None:
        if not state.is_select:
            return
        # Explicit opt-outs: a bypass block, or a query that has asked for it.
        if is_bypassed() or state.execution_options.get("skip_tenant_filter", False):
            return

        tenant_id = _current_tenant_id.get()
        if tenant_id is None:
            # Nothing to scope by. Non-tenant models (tenants, platform users)
            # are unaffected; a tenant-scoped model reaching here will fail at
            # the repository layer with TenantContextError instead of silently
            # returning another tenant's rows.
            return

        state.statement = state.statement.options(
            with_loader_criteria(
                TenantScoped,
                lambda cls: cls.tenant_id == tenant_id,
                include_aliases=True,
            )
        )

    @event.listens_for(Session, "before_flush")
    def _stamp_tenant_on_insert(session: Session, flush_context: Any, instances: Any) -> None:
        """Stamp ``tenant_id`` on new rows, and refuse cross-tenant writes."""
        if is_bypassed():
            return

        tenant_id = _current_tenant_id.get()

        for obj in session.new:
            if not isinstance(obj, TenantScoped):
                continue
            # Declared Mapped[int], but a pending instance really does hold
            # None until this hook fills it in — so read it defensively rather
            # than trusting the static type.
            current: int | None = getattr(obj, "tenant_id", None)

            if current is None:
                if tenant_id is None:
                    raise TenantContextError(
                        f"Cannot insert {type(obj).__name__} with no tenant in context."
                    )
                obj.tenant_id = tenant_id
            elif tenant_id is not None and current != tenant_id:
                raise TenantContextError(
                    f"Refusing to insert {type(obj).__name__} for tenant "
                    f"{current} while acting as tenant {tenant_id}."
                )

        # Re-parenting an existing row into another tenant is never legitimate.
        for obj in session.dirty:
            if not isinstance(obj, TenantScoped) or not session.is_modified(obj):
                continue
            if tenant_id is not None and obj.tenant_id != tenant_id:
                raise TenantContextError(
                    f"Refusing to modify {type(obj).__name__} belonging to tenant "
                    f"{obj.tenant_id} while acting as tenant {tenant_id}."
                )
