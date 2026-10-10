"""Per-tenant numbers: an order's or a client's number within its own bureau.

The id is shared by every tenant, so it cannot be what people see: a new
bureau's third order would read "#13". Each numbered model carries a
``number`` that counts 1, 2, 3 per tenant (revision 0016).

The number is drawn here, as the ORM inserts the row, so it is known at once
and works on SQLite in the tests too. On PostgreSQL a transaction-level
advisory lock per (table, tenant) makes concurrent inserts in one bureau wait
their turn, so two never draw the same number; the unique constraint on
(tenant_id, number) is the backstop. Rows inserted around the ORM (a script, a
manual INSERT) are numbered by the database trigger revision 0016 installs,
which uses the same lock.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import Connection, Table, event, func, select, text
from sqlalchemy.orm import Mapper, Session, object_session

#: Numbers handed out in the current flush, per (table, tenant), so several
#: new rows flushed together count on from each other rather than all
#: reading the same maximum.
_DRAWN = "suliko.numbers_drawn"


def _table(mapper: Mapper[Any]) -> Table:
    return cast(Table, mapper.local_table)


def _next_number(connection: Connection, mapper: Mapper[Any], tenant_id: int) -> int:
    table = _table(mapper)
    if connection.dialect.name == "postgresql":
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"{table.name}:{tenant_id}"},
        )
    highest = connection.execute(
        select(func.coalesce(func.max(table.c.number), 0)).where(table.c.tenant_id == tenant_id)
    ).scalar_one()
    return int(highest) + 1


def numbered_per_tenant(model: type[Any]) -> type[Any]:
    """Give ``model`` its per-tenant ``number`` on insert. Returns the model."""

    @event.listens_for(model, "before_insert")
    def _assign(mapper: Mapper[Any], connection: Connection, target: Any) -> None:
        if getattr(target, "number", None) is not None:
            return
        session = object_session(target)
        drawn: dict[tuple[str, int], int] = (
            session.info.setdefault(_DRAWN, {}) if session is not None else {}
        )
        key = (_table(mapper).name, target.tenant_id)
        number = max(_next_number(connection, mapper, target.tenant_id), drawn.get(key, 0) + 1)
        drawn[key] = number
        target.number = number

    return model


@event.listens_for(Session, "after_flush")
def _forget_drawn(session: Session, _flush_context: Any) -> None:
    # Once flushed, the rows are in the table and the next maximum sees them.
    session.info.pop(_DRAWN, None)
