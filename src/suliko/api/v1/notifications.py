"""The notification feed and the order comment thread.

Two related surfaces: `/notifications` is one user's bell, `/orders/{id}/
comments` is the thread that feeds it.

## No permission check on the feed

Every endpoint under `/notifications` is scoped to `session.user_id` — you can
only ever read or mark your own. There is no permission that grants access to
someone else's feed, so there is nothing to check beyond being signed in.
Comments are different: those are order data and need `orders.read`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, func, or_, select, update

from suliko.api.deps import CurrentSession, Db, require, require_feature
from suliko.api.v1._shared import PageMeta
from suliko.core.errors import NotFoundError, PermissionDeniedError
from suliko.domain.notifications import notify
from suliko.domain.plans import Feature
from suliko.models.collaboration import (
    Notification,
    NotificationKind,
    OrderComment,
    OrderCommentMention,
)
from suliko.models.directory import Client
from suliko.models.order import Order
from suliko.models.user import User
from suliko.security.permissions import Permission

# Gated on the PLAN, not on a permission: every role may read their own
# notifications, but a freelancer has no colleagues to be notified by, so
# the whole screen is withheld rather than shown permanently empty.
router = APIRouter(
    prefix="/notifications",
    tags=["notifications"],
    dependencies=[Depends(require_feature(Feature.NOTIFICATIONS))],
)
comments_router = APIRouter(prefix="/orders", tags=["comments"])


# ── Feed ────────────────────────────────────────────────────────────────────


class NotificationOut(BaseModel):
    id: int
    kind: NotificationKind
    body: str
    actor_user_id: int | None
    actor_name: str | None
    order_id: int | None
    comment_id: int | None
    subject_label: str | None
    is_read: bool
    created_at: datetime


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    meta: PageMeta
    #: Unread count across the WHOLE feed, not this page — it is the bell
    #: badge, and it must not change as you page through.
    unread: int


Tab = Literal["all", "unread", "mentions", "automated"]


def _tab_filter(stmt: Select[Any], tab: Tab) -> Select[Any]:
    if tab == "unread":
        return stmt.where(Notification.read_at.is_(None))
    if tab == "mentions":
        return stmt.where(Notification.kind == NotificationKind.MENTION)
    if tab == "automated":
        # "Automated" means nobody did it — a status change on a schedule, a
        # payment webhook. An actor-less row is exactly that.
        return stmt.where(Notification.actor_user_id.is_(None))
    return stmt


@router.get("", response_model=NotificationPage)
async def list_notifications(
    db: Db,
    session: CurrentSession,
    tab: Tab = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NotificationPage:
    mine = Notification.user_id == session.user_id

    stmt = _tab_filter(select(Notification).where(mine), tab)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    rows = (
        (
            await db.execute(
                stmt.order_by(Notification.created_at.desc(), Notification.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    unread = (
        await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(mine, Notification.read_at.is_(None))
        )
        or 0
    )

    return NotificationPage(
        items=[
            NotificationOut(
                id=row.id,
                kind=row.kind,
                body=row.body,
                actor_user_id=row.actor_user_id,
                actor_name=row.actor_name,
                order_id=row.order_id,
                comment_id=row.comment_id,
                subject_label=row.subject_label,
                is_read=row.read_at is not None,
                created_at=row.created_at,
            )
            for row in rows
        ],
        meta=PageMeta(total=int(total), limit=limit, offset=offset),
        unread=int(unread),
    )


class UnreadCount(BaseModel):
    unread: int


@router.get("/unread-count", response_model=UnreadCount)
async def unread_count(db: Db, session: CurrentSession) -> UnreadCount:
    """The bell badge. Cheap enough to poll."""
    count = (
        await db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.user_id == session.user_id,
                Notification.read_at.is_(None),
            )
        )
        or 0
    )
    return UnreadCount(unread=int(count))


class MarkedRead(BaseModel):
    marked: int
    unread: int


@router.post("/read-all", response_model=MarkedRead)
async def mark_all_read(db: Db, session: CurrentSession) -> MarkedRead:
    unread = Notification.read_at.is_(None)
    mine = Notification.user_id == session.user_id

    # Counted before the update rather than read off `rowcount`: the async
    # cursor does not expose one, and this is a single extra index scan.
    pending = await db.scalar(select(func.count()).select_from(Notification).where(mine, unread))

    await db.execute(update(Notification).where(mine, unread).values(read_at=datetime.now(UTC)))
    return MarkedRead(marked=int(pending or 0), unread=0)


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: int,
    db: Db,
    session: CurrentSession,
) -> NotificationOut:
    row = await db.get(Notification, notification_id)
    # Someone else's notification is reported as missing, not forbidden: the
    # id space is shared, and a 403 would confirm the row exists.
    if row is None or row.user_id != session.user_id:
        raise NotFoundError("Notification not found.")

    if row.read_at is None:
        row.read_at = datetime.now(UTC)
        await db.flush()

    return NotificationOut(
        id=row.id,
        kind=row.kind,
        body=row.body,
        actor_user_id=row.actor_user_id,
        actor_name=row.actor_name,
        order_id=row.order_id,
        comment_id=row.comment_id,
        subject_label=row.subject_label,
        is_read=True,
        created_at=row.created_at,
    )


# ── Order comments ──────────────────────────────────────────────────────────

#: `@username`, where a username may be a whole email address — invites and
#: sign-up use the email as the username, so "@nino@acme.ge" must capture all
#: of it rather than stop at the second "@". "@nino" (the part before the @)
#: also works when exactly one colleague's username starts that way.
MENTION_PATTERN = re.compile(r"@([A-Za-z0-9._%+-]{2,100}(?:@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)?)")


def mention_names(body: str) -> set[str]:
    """The mentioned names, lower-cased, without trailing sentence dots."""
    return {name.rstrip(".").lower() for name in MENTION_PATTERN.findall(body)} - {""}


def resolve_mentions(names: set[str], users: list[User]) -> list[User]:
    """Match names to users: a full username, or an unambiguous local part."""
    by_username = {user.username.lower(): user for user in users}
    found: dict[int, User] = {}
    for name in names:
        exact = by_username.get(name)
        if exact is not None:
            found[exact.id] = exact
            continue
        if "@" not in name:
            local = [u for u in users if u.username.lower().split("@", 1)[0] == name]
            if len(local) == 1:
                found[local[0].id] = local[0]
    return list(found.values())


class CommentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=5000)
    is_pinned: bool = False


class CommentOut(BaseModel):
    id: int
    order_id: int
    author_user_id: int | None
    author_name: str
    body: str
    is_pinned: bool
    mentions: list[str] = Field(default_factory=list)
    created_at: datetime


async def _order_or_404(db: Db, order_id: int) -> Order:
    order = await db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found.")
    return order


async def _subject_label(db: Db, order: Order) -> str:
    """`{client} #{order}` — the line under a feed entry."""
    client = await db.get(Client, order.client_id)
    return f"{client.name if client else 'Unknown'} #{order.id}"


@comments_router.get("/{order_id}/comments", response_model=list[CommentOut])
async def list_comments(
    order_id: int,
    db: Db,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> list[CommentOut]:
    await _order_or_404(db, order_id)

    rows = (
        (
            await db.execute(
                select(OrderComment)
                .where(
                    OrderComment.order_id == order_id,
                    OrderComment.deleted_at.is_(None),
                )
                # Pinned first, then newest — matching the PHP thread.
                .order_by(OrderComment.is_pinned.desc(), OrderComment.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    if not rows:
        return []

    mention_rows = (
        await db.execute(
            select(OrderCommentMention.comment_id, User.username)
            .join(User, User.id == OrderCommentMention.user_id)
            .where(OrderCommentMention.comment_id.in_([row.id for row in rows]))
        )
    ).all()
    by_comment: dict[int, list[str]] = {}
    for comment_id, username in mention_rows:
        by_comment.setdefault(comment_id, []).append(username)

    return [
        CommentOut(
            id=row.id,
            order_id=row.order_id,
            author_user_id=row.author_user_id,
            author_name=row.author_name,
            body=row.body,
            is_pinned=row.is_pinned,
            mentions=by_comment.get(row.id, []),
            created_at=row.created_at,
        )
        for row in rows
    ]


@comments_router.post(
    "/{order_id}/comments", response_model=CommentOut, status_code=http_status.HTTP_201_CREATED
)
async def create_comment(
    order_id: int,
    payload: CommentIn,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.ORDERS_READ))],
) -> CommentOut:
    """Leave an internal note on an order.

    Gated on `orders.read`, not `orders.write`: commenting is how staff who can
    see a job discuss it, and requiring write here would stop the people most
    likely to be asking a question.
    """
    order = await _order_or_404(db, order_id)

    comment = OrderComment(
        order_id=order_id,
        author_user_id=session.user_id,
        author_name=session.full_name or session.username,
        body=payload.body,
        is_pinned=payload.is_pinned,
    )
    db.add(comment)
    await db.flush()

    # Resolve @mentions to real users. Unknown names are left as plain text —
    # a typo should not silently become a mention of nobody.
    names = mention_names(payload.body)
    mentioned: list[User] = []
    if names:
        candidates = list(
            (
                await db.execute(
                    select(User).where(
                        or_(
                            func.lower(User.username).in_(names),
                            *[
                                func.lower(User.username).like(f"{name}@%")
                                for name in names
                                if "@" not in name
                            ],
                        ),
                        User.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        mentioned = resolve_mentions(names, candidates)

    for user in mentioned:
        if user.id == session.user_id:
            continue
        db.add(OrderCommentMention(comment_id=comment.id, order_id=order_id, user_id=user.id))

    label = await _subject_label(db, order)
    excerpt = payload.body if len(payload.body) <= 120 else f"{payload.body[:117]}…"

    mentioned_ids = [user.id for user in mentioned if user.id != session.user_id]
    if mentioned_ids:
        await notify(
            db,
            user_ids=mentioned_ids,
            kind=NotificationKind.MENTION,
            body=f"mentioned you: {excerpt}",
            actor_user_id=session.user_id,
            actor_name=session.full_name or session.username,
            order_id=order_id,
            comment_id=comment.id,
            subject_label=label,
        )

    # Everyone else who has already taken part in this thread gets a plain
    # comment notification. Deliberately not the whole office: an order with
    # one long thread would otherwise spam people who never touched it.
    participants = (
        (
            await db.execute(
                select(OrderComment.author_user_id).where(
                    OrderComment.order_id == order_id,
                    OrderComment.author_user_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    others = [
        user_id
        for user_id in dict.fromkeys(participants)
        if user_id is not None and user_id != session.user_id and user_id not in mentioned_ids
    ]
    if others:
        await notify(
            db,
            user_ids=others,
            kind=NotificationKind.COMMENT,
            body=f"commented: {excerpt}",
            actor_user_id=session.user_id,
            actor_name=session.full_name or session.username,
            order_id=order_id,
            comment_id=comment.id,
            subject_label=label,
        )

    await db.flush()

    return CommentOut(
        id=comment.id,
        order_id=order_id,
        author_user_id=comment.author_user_id,
        author_name=comment.author_name,
        body=comment.body,
        is_pinned=comment.is_pinned,
        mentions=[user.username for user in mentioned],
        created_at=comment.created_at,
    )


class CommentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_pinned: bool


@comments_router.patch("/{order_id}/comments/{comment_id}", response_model=CommentOut)
async def pin_comment(
    order_id: int,
    comment_id: int,
    payload: CommentPatch,
    db: Db,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> CommentOut:
    """Pin or unpin. Body text is deliberately not editable.

    An internal note is a record of what someone said at the time; letting it
    be rewritten after the fact would make the thread useless as evidence of
    who agreed to what.
    """
    comment = await db.get(OrderComment, comment_id)
    if comment is None or comment.order_id != order_id or comment.deleted_at is not None:
        raise NotFoundError("Comment not found.")

    comment.is_pinned = payload.is_pinned
    await db.flush()

    return CommentOut(
        id=comment.id,
        order_id=comment.order_id,
        author_user_id=comment.author_user_id,
        author_name=comment.author_name,
        body=comment.body,
        is_pinned=comment.is_pinned,
        mentions=[],
        created_at=comment.created_at,
    )


@comments_router.delete(
    "/{order_id}/comments/{comment_id}", status_code=http_status.HTTP_204_NO_CONTENT
)
async def delete_comment(
    order_id: int,
    comment_id: int,
    db: Db,
    session: Annotated[CurrentSession, Depends(require(Permission.ORDERS_READ))],
) -> None:
    """Soft-delete your own comment.

    Anyone with `orders.delete` can remove someone else's — that is the
    moderation case. Everyone else can only remove their own, so a disagreement
    cannot be quietly erased by the other party.
    """
    comment = await db.get(OrderComment, comment_id)
    if comment is None or comment.order_id != order_id or comment.deleted_at is not None:
        raise NotFoundError("Comment not found.")

    if comment.author_user_id != session.user_id and not session.has(Permission.ORDERS_DELETE):
        raise PermissionDeniedError("You can only delete your own comments.")

    comment.deleted_at = datetime.now(UTC)

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="comment.deleted",
        entity_type="order_comment",
        entity_id=comment.id,
        before={"order_id": comment.order_id, "body": comment.body},
    )
    await db.flush()
