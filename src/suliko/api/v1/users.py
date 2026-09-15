"""Staff users.

This router is where privilege escalation would happen if it were going to, so
several rules are enforced that are not obvious from the data model:

- You cannot change your own role. Otherwise `users.manage` is a one-step path
  from admin to owner, and the permission bundles stop meaning anything.
- You cannot assign a role above your own. Same reason.
- The last active owner cannot be demoted, deactivated or deleted, or the
  tenant is left with nobody who can administer it.
- `superuser` cannot be granted here at all — it is platform-level and is
  created out-of-band by the CLI.
- A role change or a password reset revokes that user's sessions, so a
  demotion takes effect immediately rather than at their next login.

Password hashes are never returned, and never accepted from the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, or_, select

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._shared import PageMeta
from suliko.core.errors import ConflictError, NotFoundError, ValidationError
from suliko.models.user import Role, User
from suliko.security.passwords import hash_password, validate_password_strength
from suliko.security.permissions import Permission
from suliko.security.sessions import revoke_all_for_user

router = APIRouter(prefix="/users", tags=["users"])

#: Roles assignable through the API. `superuser` is deliberately absent —
#: it is platform-level and only the CLI creates it.
ASSIGNABLE_ROLES = (Role.OWNER, Role.ADMIN, Role.MANAGER, Role.STAFF)

#: Seniority, for the "cannot assign above your own role" check.
RANK: dict[Role, int] = {
    Role.STAFF: 1,
    Role.MANAGER: 2,
    Role.ADMIN: 3,
    Role.OWNER: 4,
    Role.SUPERUSER: 5,
}


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=12, max_length=1024)
    role: Role = Role.STAFF


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None = None
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    role: Role | None = None
    is_active: bool | None = None


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=12, max_length=1024)


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    full_name: str
    role: Role
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class UserPage(BaseModel):
    items: list[UserOut]
    meta: PageMeta


def _out(row: User) -> UserOut:
    return UserOut(
        id=row.id,
        username=row.username,
        email=row.email,
        full_name=row.full_name,
        role=row.role,
        is_active=row.is_active,
        last_login_at=row.last_login_at,
        created_at=row.created_at,
    )


async def _active_owner_count(db: Db) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == Role.OWNER, User.is_active.is_(True))
        )
    ) or 0


def _guard_assignable(actor_role: Role, target_role: Role) -> None:
    if target_role is Role.SUPERUSER:
        raise ValidationError(
            "Superuser cannot be assigned here. It is created from the server console."
        )
    if RANK[target_role] > RANK[actor_role]:
        raise ValidationError("You cannot assign a role above your own.")


@router.get("", response_model=UserPage)
async def list_users(
    db: Db,
    _: Annotated[object, Depends(require(Permission.USERS_MANAGE))],
    search: Annotated[str | None, Query(max_length=255)] = None,
    role: Role | None = None,
    is_active: bool | None = None,
    sort: Literal["username", "-username", "id", "-id"] = "username",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserPage:
    stmt = select(User)

    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                User.username.ilike(pattern),
                User.email.ilike(pattern),
                User.full_name.ilike(pattern),
            )
        )
    if role is not None:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    column = User.username if sort.lstrip("-") == "username" else User.id
    stmt = stmt.order_by(column.desc() if sort.startswith("-") else column.asc())

    rows = (await db.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return UserPage(
        items=[_out(r) for r in rows], meta=PageMeta(total=total, limit=limit, offset=offset)
    )


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: int,
    db: Db,
    _: Annotated[object, Depends(require(Permission.USERS_MANAGE))],
) -> UserOut:
    row = await db.get(User, user_id)
    if row is None:
        raise NotFoundError("User not found.")
    return _out(row)


@router.post("", response_model=UserOut, status_code=http_status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.USERS_MANAGE))],
) -> UserOut:
    _guard_assignable(session.role, payload.role)

    problems = validate_password_strength(payload.password)
    if problems:
        raise ValidationError(" ".join(problems))

    clash = (
        (
            await db.execute(
                select(User).where(
                    or_(User.username == payload.username, User.email == payload.email)
                )
            )
        )
        .scalars()
        .first()
    )
    if clash:
        # Not an enumeration risk: only users.manage reaches this, and they can
        # already list everyone.
        raise ConflictError("That username or email is already in use.")

    row = User(
        username=payload.username,
        email=payload.email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_active=True,
    )
    db.add(row)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="user.created",
        entity_type="user",
        entity_id=row.id,
        after={"username": payload.username, "role": payload.role.value},
    )
    return _out(row)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.USERS_MANAGE))],
) -> UserOut:
    row = await db.get(User, user_id)
    if row is None:
        raise NotFoundError("User not found.")

    changes = payload.model_dump(exclude_unset=True)
    before = {k: getattr(row, k) for k in changes}

    role_changed = "role" in changes and changes["role"] != row.role
    deactivating = changes.get("is_active") is False and row.is_active

    if role_changed:
        if row.id == session.user_id:
            raise ValidationError("You cannot change your own role. Ask another administrator.")
        _guard_assignable(session.role, changes["role"])

    # The tenant must keep at least one active owner who can administer it.
    losing_owner = row.role is Role.OWNER and (
        deactivating or (role_changed and changes["role"] is not Role.OWNER)
    )
    if losing_owner and await _active_owner_count(db) <= 1:
        raise ConflictError("This is the last active owner. Promote someone else first.")

    for field, value in changes.items():
        setattr(row, field, value)
    await db.flush()

    # A demotion or a deactivation must bite immediately, not at next login.
    if role_changed or deactivating:
        await revoke_all_for_user(db, row.id)

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="user.updated",
        entity_type="user",
        entity_id=row.id,
        before=before,
        after=changes,
    )
    return _out(row)


@router.post("/{user_id}/password", status_code=http_status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: int,
    payload: PasswordReset,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.USERS_MANAGE))],
) -> None:
    """Set another user's password.

    Every session of theirs is revoked: if this is being used because an
    account was compromised, leaving the attacker's session alive would defeat
    the point.
    """
    row = await db.get(User, user_id)
    if row is None:
        raise NotFoundError("User not found.")

    problems = validate_password_strength(payload.password)
    if problems:
        raise ValidationError(" ".join(problems))

    row.password_hash = hash_password(payload.password)
    await db.flush()
    await revoke_all_for_user(db, row.id)

    from suliko.core.audit import record

    # The password itself is never logged — core.crypto.redact would strip it,
    # but it is simply not passed in the first place.
    await record(db, session, action="user.password_reset", entity_type="user", entity_id=row.id)


@router.delete("/{user_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.USERS_MANAGE))],
) -> None:
    """Delete a user.

    Deactivating is usually better — it keeps `changed_by` attribution on the
    status history readable. Deletion is for accounts created by mistake.
    """
    row = await db.get(User, user_id)
    if row is None:
        raise NotFoundError("User not found.")

    if row.id == session.user_id:
        raise ValidationError("You cannot delete your own account.")

    if row.role is Role.OWNER and await _active_owner_count(db) <= 1:
        raise ConflictError("This is the last active owner and cannot be deleted.")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="user.deleted",
        entity_type="user",
        entity_id=row.id,
        before={"username": row.username, "role": row.role.value},
    )
    await db.delete(row)
