"""Creating notifications.

One place that writes `Notification` rows, so the feed's shape is decided here
rather than in each router that happens to trigger one.

## Fan-out at write time

A notification is written once per recipient. The alternative — computing the
feed per request from comments, payments and status events — has no place to
store "read by this user", and the feed is read far more often than it is
written.

## Never notify the actor

Someone who just left a comment does not need to be told they left a comment.
Every helper here drops the actor from the recipient list, which is also what
keeps the unread badge from incrementing on your own action.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.domain.plans import TenantPlan, effective_permissions
from suliko.models.collaboration import Notification, NotificationKind
from suliko.models.user import User, UserPermissionOverride
from suliko.security.permissions import Permission


async def _active_user_ids(db: AsyncSession, exclude: int | None = None) -> list[int]:
    """Every active user in the current tenant, minus one.

    Tenant-scoped implicitly: `User` is `TenantScoped`, so the ORM filter adds
    the predicate. See `db/tenancy.py`.
    """
    rows = (await db.execute(select(User.id).where(User.is_active.is_(True)))).scalars().all()
    return [user_id for user_id in rows if user_id != exclude]


async def notify(
    db: AsyncSession,
    *,
    user_ids: list[int],
    kind: NotificationKind,
    body: str,
    actor_user_id: int | None = None,
    actor_name: str | None = None,
    order_id: int | None = None,
    comment_id: int | None = None,
    subject_label: str | None = None,
) -> int:
    """Write one notification per recipient. Returns how many were written.

    Does not flush — the caller's transaction decides. A notification about an
    action that then rolls back must not survive the action it describes.
    """
    recipients = [user_id for user_id in dict.fromkeys(user_ids) if user_id != actor_user_id]

    for user_id in recipients:
        db.add(
            Notification(
                user_id=user_id,
                kind=kind,
                body=body[:500],
                actor_user_id=actor_user_id,
                actor_name=actor_name,
                order_id=order_id,
                comment_id=comment_id,
                subject_label=subject_label,
            )
        )
    return len(recipients)


async def notify_permitted(
    db: AsyncSession,
    *,
    permission: Permission,
    plan: TenantPlan,
    kind: NotificationKind,
    body: str,
    actor_user_id: int | None = None,
    actor_name: str | None = None,
    order_id: int | None = None,
    subject_label: str | None = None,
) -> int:
    """Notify every active user who holds `permission`, except the actor.

    For events whose content is itself restricted. "Payment received - 450
    GEL" broadcast to the whole office tells staff without `finance.read`
    exactly what the Finances screen withholds from them. Effective access is
    role, then overrides, then plan — the same computation a request makes.
    """
    users = (await db.execute(select(User.id, User.role).where(User.is_active.is_(True)))).all()
    ids = [int(user_id) for user_id, _role in users]
    overrides: dict[int, dict[str, bool]] = {}
    if ids:
        for row in (
            await db.execute(
                select(UserPermissionOverride).where(UserPermissionOverride.user_id.in_(ids))
            )
        ).scalars():
            overrides.setdefault(row.user_id, {})[row.permission] = row.granted

    recipients = [
        int(user_id)
        for user_id, role in users
        if permission in effective_permissions(role, plan, overrides.get(int(user_id), {}))
    ]
    return await notify(
        db,
        user_ids=recipients,
        kind=kind,
        body=body,
        actor_user_id=actor_user_id,
        actor_name=actor_name,
        order_id=order_id,
        subject_label=subject_label,
    )


async def notify_everyone(
    db: AsyncSession,
    *,
    kind: NotificationKind,
    body: str,
    actor_user_id: int | None = None,
    actor_name: str | None = None,
    order_id: int | None = None,
    subject_label: str | None = None,
) -> int:
    """Notify every active user in the tenant except the actor.

    Used for office-wide events — a new order, a payment received. Fine at the
    current scale (a bureau has single-digit staff); if a tenant ever has
    hundreds of users this needs a subscription model rather than a broadcast.
    """
    return await notify(
        db,
        user_ids=await _active_user_ids(db, exclude=actor_user_id),
        kind=kind,
        body=body,
        actor_user_id=actor_user_id,
        actor_name=actor_name,
        order_id=order_id,
        subject_label=subject_label,
    )
