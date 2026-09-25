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

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, or_, select

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.v1._shared import PageMeta
from suliko.config import get_settings
from suliko.core import mail
from suliko.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    ValidationError,
)
from suliko.core.ratelimit import RateLimiter, get_rate_limiter
from suliko.domain.plans import (
    NON_OVERRIDABLE,
    TenantPlan,
    effective_permissions,
    overridable_for_plan,
    permissions_for_plan,
)
from suliko.domain.portal import account_matches, normalize_email, normalize_phone, registration_url
from suliko.models.portal import InviteKind, InviteStatus, PortalAccountInvite, PortalTranslator
from suliko.models.user import Role, User, UserPermissionOverride
from suliko.security import reset_tokens
from suliko.security.passwords import (
    generate_token,
    hash_password_async,
    validate_password_strength,
)
from suliko.security.permissions import Permission, permissions_for_role
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
    position: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=50)
    role: Role | None = None
    is_active: bool | None = None
    #: The WHOLE set this person should end up with, not a delta. Omit the
    #: field to leave their access alone; send a list to replace it.
    #:
    #: Absent vs empty matters here and the distinction is why this is
    #: `None`-defaulted rather than an empty list: `[]` means "this person may
    #: do nothing", which is a real and useful state for a suspended employee.
    permissions: list[str] | None = None


class UserInvite(BaseModel):
    """The owner's invite form, field for field."""

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    position: str | None = Field(default=None, max_length=100)
    phone: str | None = Field(default=None, max_length=50)
    #: The bundle their access STARTS from. `permissions` below then says
    #: exactly what they end up with; the role remains as the label on the
    #: Users screen and in the audit log.
    role: Role = Role.STAFF
    permissions: list[str] | None = None


class SulikoAccountOut(BaseModel):
    """Whether this invite's email or phone matched a suliko.ge account.

    'linked' means exactly one account matched at invite time; 'pending'
    means none did (or more than one), and stays that way until
    `domain.portal.resolve_pending_invites` finds a match — see that
    function's docstring for the two moments that can happen. A CRM login is
    not the suliko.ge portal, so 'linked' here is a record of identity, not a
    functional connection the way it is for a translator invite.
    """

    status: Literal["linked", "pending"]
    matched_display_name: str | None


class InviteOut(BaseModel):
    user: UserOut
    #: The set-your-own-password link, shown to the inviter ONCE.
    #:
    #: Returned so a bounced or delayed email is not a dead end: the inviter
    #: can pass the link on themselves. It grants them nothing new (they can
    #: already set this person's password through `POST /users/{id}/password`)
    #: and, unlike the one-time password it replaces, it works once and
    #: expires — a password in an inbox stayed valid until someone changed it.
    invite_link: str
    invite_expires_at: datetime
    #: Whether the email actually left. False is not an error: the account
    #: exists either way, and the link above is the fallback.
    email_sent: bool
    suliko_account: SulikoAccountOut


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=12, max_length=1024)


class UserOut(BaseModel):
    id: int
    username: str
    email: str
    full_name: str
    position: str | None
    phone: str | None
    role: Role
    is_active: bool
    #: Still on the password they were handed. Shown on the Users screen as
    #: "invited" — an account in this state has never been used.
    must_change_password: bool
    #: What this person may ACTUALLY do: role, then their overrides, then the
    #: tenant's plan. Not the role's bundle — that would show the owner a list
    #: the API does not agree with.
    permissions: list[str]
    last_login_at: datetime | None
    created_at: datetime
    #: Null for anyone created through `POST /users` — nobody ever asked
    #: suliko.ge about them. Everyone invited through `POST /users/invite` has
    #: one, `linked` or `pending`.
    suliko_account: SulikoAccountOut | None


class UserPage(BaseModel):
    items: list[UserOut]
    meta: PageMeta


def _out(
    row: User,
    plan: TenantPlan,
    overrides: Mapping[str, bool],
    suliko_account: SulikoAccountOut | None = None,
) -> UserOut:
    return UserOut(
        id=row.id,
        username=row.username,
        email=row.email,
        full_name=row.full_name,
        position=row.position,
        phone=row.phone,
        role=row.role,
        is_active=row.is_active,
        must_change_password=row.must_change_password,
        permissions=sorted(p.value for p in effective_permissions(row.role, plan, overrides)),
        last_login_at=row.last_login_at,
        created_at=row.created_at,
        suliko_account=suliko_account,
    )


async def _overrides_for(db: Db, user_ids: Sequence[int]) -> dict[int, dict[str, bool]]:
    """Every override for a set of users, keyed by user.

    One query for the whole page rather than one per row — a twenty-row Users
    screen should not be twenty-one round trips.
    """
    if not user_ids:
        return {}

    rows = (
        (
            await db.execute(
                select(UserPermissionOverride).where(
                    UserPermissionOverride.user_id.in_(list(user_ids))
                )
            )
        )
        .scalars()
        .all()
    )

    out: dict[int, dict[str, bool]] = {}
    for row in rows:
        out.setdefault(row.user_id, {})[row.permission] = row.granted
    return out


async def _invite_status_for(db: Db, user_ids: Sequence[int]) -> dict[int, SulikoAccountOut]:
    """The suliko.ge match recorded against each user's invite, keyed by user.

    Same shape as `_overrides_for` and for the same reason. Only users invited
    through `POST /users/invite` have a row here — `POST /users` never asks
    suliko.ge anything, so a user created that way is simply absent from the
    result, and the caller treats a miss as `None`.
    """
    if not user_ids:
        return {}

    rows = (
        await db.execute(
            select(PortalAccountInvite, PortalTranslator.display_name)
            .outerjoin(
                PortalTranslator,
                PortalTranslator.id == PortalAccountInvite.portal_translator_id,
            )
            .where(
                PortalAccountInvite.kind == InviteKind.STAFF,
                PortalAccountInvite.user_id.in_(list(user_ids)),
            )
        )
    ).all()

    return {
        invite.user_id: SulikoAccountOut(
            status="linked" if invite.status is InviteStatus.LINKED else "pending",
            matched_display_name=matched_display_name,
        )
        for invite, matched_display_name in rows
        if invite.user_id is not None
    }


def _requested_permissions(names: Sequence[str], plan: TenantPlan) -> set[Permission]:
    """Validate what the form asked for.

    Rejects rather than silently dropping. An owner who ticks a box the plan
    does not include and gets a saved-with-no-effect result has been told
    nothing; a 422 naming the permission tells them to upgrade.
    """
    allowed = overridable_for_plan(plan)
    requested: set[Permission] = set()

    for name in names:
        try:
            permission = Permission(name)
        except ValueError:
            raise ValidationError(f"Unknown permission: {name}") from None
        if permission in NON_OVERRIDABLE:
            # The one that would be an escalation out of the tenant.
            raise ValidationError(f"{name} cannot be granted to a user.")
        if permission not in allowed:
            raise ValidationError(f"{name} is not included in the {plan.value} plan.")
        requested.add(permission)

    return requested


async def _write_overrides(
    db: Db,
    user: User,
    requested: AbstractSet[Permission],
    *,
    plan: TenantPlan,
) -> None:
    """Store the DIFFERENCE between what was asked for and the role's bundle.

    Storing the difference rather than the set is what keeps a later role
    change meaningful — see `UserPermissionOverride`. It also means an owner
    who ticks exactly the role's defaults leaves no rows behind at all.

    Replaces wholesale: the form submits the complete desired set, so any row
    not implied by it is stale by definition.
    """
    from_role = permissions_for_role(user.role) & permissions_for_plan(plan)

    grants = requested - from_role
    revokes = from_role - requested

    existing = (
        (
            await db.execute(
                select(UserPermissionOverride).where(UserPermissionOverride.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await db.delete(row)
    await db.flush()

    for permission in sorted(grants):
        db.add(UserPermissionOverride(user_id=user.id, permission=permission.value, granted=True))
    for permission in sorted(revokes):
        db.add(UserPermissionOverride(user_id=user.id, permission=permission.value, granted=False))
    await db.flush()


async def _active_owner_count(db: Db) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == Role.OWNER, User.is_active.is_(True))
        )
    ) or 0


def _guard_grantable(session: CurrentSession, requested: AbstractSet[Permission]) -> None:
    """Nobody may hand out a permission they do not themselves hold.

    Without this, `users.manage` is a full escalation: an admin who cannot
    make bank transfers could grant `finance.transfer` to an account they
    control and use it. The plan is already accounted for — `session.permissions`
    has been intersected with it — so this compares against what the actor can
    actually do today, not against what their role nominally allows.
    """
    beyond = requested - set(session.permissions)
    if beyond:
        raise ValidationError(
            "You cannot grant access you do not have yourself: "
            + ", ".join(sorted(p.value for p in beyond))
        )


def _guard_assignable(actor_role: Role, target_role: Role) -> None:
    if target_role is Role.SUPERUSER:
        raise ValidationError(
            "Superuser cannot be assigned here. It is created from the server console."
        )
    if RANK[target_role] > RANK[actor_role]:
        raise ValidationError("You cannot assign a role above your own.")


def _guard_target(session: CurrentSession, row: User) -> None:
    """Nobody may act on an account at or above their own rank.

    `users.manage` reaches every row in the tenant, so without this an admin
    could set the owner's password (or change the owner's email and use
    "forgot password") and sign in as them — the role guards above only stop
    someone changing a ROLE, not taking over the account that holds it.

    Owners are the one exception to "same rank": co-owners have to be able to
    manage each other, or a departed owner could never be deactivated. A
    superuser row is never touched from a tenant screen at all — it is created
    and maintained from the server console.

    Acting on yourself is not decided here; the handlers carry their own
    self-rules (no own role, own access, own deactivation or own deletion).
    """
    if row.id == session.user_id:
        return
    if row.role is Role.SUPERUSER:
        raise PermissionDeniedError("A platform account cannot be changed from here.")
    actor, target = RANK[session.role], RANK[row.role]
    if target > actor or (target == actor and session.role is not Role.OWNER):
        raise PermissionDeniedError("You cannot change an account at or above your own role.")


@router.get("", response_model=UserPage)
async def list_users(
    db: Db,
    session: CurrentSession,
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
    overrides = await _overrides_for(db, [r.id for r in rows])
    invites = await _invite_status_for(db, [r.id for r in rows])
    return UserPage(
        items=[_out(r, session.plan, overrides.get(r.id, {}), invites.get(r.id)) for r in rows],
        meta=PageMeta(total=total, limit=limit, offset=offset),
    )


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: int,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.USERS_MANAGE))],
) -> UserOut:
    row = await db.get(User, user_id)
    if row is None:
        raise NotFoundError("User not found.")
    overrides = (await _overrides_for(db, [row.id])).get(row.id, {})
    suliko_account = (await _invite_status_for(db, [row.id])).get(row.id)
    return _out(row, session.plan, overrides, suliko_account)


def _suliko_account_paragraph(*, linked: bool, matched_display_name: str | None) -> str:
    """The one paragraph `_invite_email` gains for this feature.

    Placed right after the sign-in credentials: how to get into THIS account,
    then what suliko.ge account this address is also expected to have.
    """
    if linked:
        matched = f" ({matched_display_name})" if matched_display_name else ""
        return (
            f"This address is already registered on suliko.ge{matched}, "
            "so nothing more to do there.\n"
        )
    return (
        "This invitation also expects a suliko.ge account under this exact "
        f"email address. If you don't have one yet, register here: {registration_url()}\n"
    )


def _invite_email(
    user: User,
    tenant_name: str,
    tenant_slug: str,
    invite_link: str,
    inviter: str,
    valid_days: int,
    *,
    suliko_account: SulikoAccountOut,
) -> tuple[str, str]:
    """Subject and plain-text body. Everything they need in one message."""
    account_paragraph = _suliko_account_paragraph(
        linked=suliko_account.status == "linked",
        matched_display_name=suliko_account.matched_display_name,
    )
    body = (
        f"Hello {user.full_name},\n\n"
        f"{inviter} has added you to {tenant_name} on Suliko.\n\n"
        f"Choose your password here:\n\n{invite_link}\n\n"
        f"The link works once and expires in {valid_days} days. After that, "
        "sign in with:\n\n"
        f"  Organisation: {tenant_slug}\n"
        f"  Username:     {user.username}\n\n"
        f"{account_paragraph}\n"
        f"If you were not expecting this, tell {tenant_name} and ignore the "
        "message; nobody can use the account without the link above.\n"
    )
    return f"You have been added to {tenant_name} on Suliko", body


@router.post("/invite", response_model=InviteOut, status_code=http_status.HTTP_201_CREATED)
async def invite_user(
    payload: UserInvite,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.USERS_MANAGE))],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> InviteOut:
    """Add someone to the bureau and email them a way in.

    The difference from `POST /users` is who chooses the password. Here the
    invitee does: the account is created with a password nobody knows, and the
    email carries a single-use, expiring set-password link (the reset-token
    machinery, with a longer life). Nothing that works as a credential sits in
    an inbox indefinitely, and nobody else ever knows the password.

    The username is the email address, matching sign-up. Both are unique per
    tenant, so the clash below is a genuine "already invited", not a
    collision with another bureau.
    """
    _guard_assignable(session.role, payload.role)

    tenant_key = f"invite:tenant:{session.tenant_id}"
    if retry := await limiter.check_invite(tenant_key):
        raise RateLimitedError(
            "This organisation has sent too many invitations today. Try again tomorrow.",
            retry_after=retry,
        )

    email = str(payload.email).strip().lower()
    username = email if len(email) <= 100 else email.split("@")[0][:100]

    clash = (
        (await db.execute(select(User).where(or_(User.username == username, User.email == email))))
        .scalars()
        .first()
    )
    if clash:
        raise ConflictError("Somebody with that email is already in this organisation.")

    # Validated before the row is written, so a rejected permission does not
    # leave a half-created account behind.
    requested = (
        _requested_permissions(payload.permissions, session.plan)
        if payload.permissions is not None
        else permissions_for_role(payload.role) & permissions_for_plan(session.plan)
    )
    _guard_grantable(session, requested)

    # A password nobody knows, so the account is unusable until the link is.
    unusable_password = generate_token()

    phone = (payload.phone or "").strip() or None
    row = User(
        username=username,
        email=email,
        full_name=payload.full_name.strip(),
        position=(payload.position or "").strip() or None,
        phone=phone,
        password_hash=await hash_password_async(unusable_password),
        role=payload.role,
        is_active=True,
        must_change_password=True,
    )
    db.add(row)
    await db.flush()

    await _write_overrides(db, row, requested, plan=session.plan)

    # Whether this address (or phone) belongs to a suliko.ge account — see
    # `domain.portal.account_matches`. A CRM login is not the suliko.ge
    # portal, so a match is recorded, not acted on: no directory row, no
    # `PortalTranslatorLink`. `db` doubles as the platform session here for
    # the same reason `api/v1/translators.py` documents at its invite route.
    matches = await account_matches(db, phone=phone, email=email)
    matched_display_name: str | None = None
    invite = PortalAccountInvite(
        tenant_id=session.tenant_id,
        kind=InviteKind.STAFF,
        user_id=row.id,
        full_name=row.full_name,
        email=email,
        phone=phone,
        normalized_email=normalize_email(email),
        normalized_phone=normalize_phone(phone),
        invited_by_user_id=session.user_id,
    )
    if len(matches) == 1:
        account, _reason = matches[0]
        matched_display_name = account.display_name
        invite.portal_translator_id = account.id
        invite.status = InviteStatus.LINKED
        invite.resolved_at = datetime.now(UTC)
    else:
        invite.status = InviteStatus.PENDING
    db.add(invite)
    await db.flush()

    suliko_account = SulikoAccountOut(
        status="linked" if invite.status is InviteStatus.LINKED else "pending",
        matched_display_name=matched_display_name,
    )

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="user.invited",
        entity_type="user",
        entity_id=row.id,
        # The password is not passed in. `redact` would strip it, but the
        # safest way to keep a secret out of a log is not to hand it over.
        after={
            "username": username,
            "role": payload.role.value,
            "position": row.position,
            "permissions": sorted(p.value for p in requested),
            "suliko_account_status": suliko_account.status,
        },
    )

    settings = get_settings()
    ttl_seconds = settings.invite_link_ttl_hours * 3600
    token = await reset_tokens.issue(db, row, ttl_seconds=ttl_seconds)
    invite_link = (
        f"{settings.app_url.rstrip('/')}/{session.tenant_locale}/reset-password"
        f"?token={quote(token, safe='')}&invite=1&org={quote(session.tenant_slug, safe='')}"
    )
    subject, body = _invite_email(
        row,
        session.tenant_name,
        session.tenant_slug,
        invite_link,
        session.full_name,
        max(1, settings.invite_link_ttl_hours // 24),
        suliko_account=suliko_account,
    )
    await limiter.record_invite(tenant_key)
    result = await mail.send(email, subject, body)

    overrides = (await _overrides_for(db, [row.id])).get(row.id, {})
    return InviteOut(
        user=_out(row, session.plan, overrides, suliko_account),
        invite_link=invite_link,
        invite_expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        email_sent=result.delivered,
        suliko_account=suliko_account,
    )


@router.post("", response_model=UserOut, status_code=http_status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.USERS_MANAGE))],
) -> UserOut:
    _guard_assignable(session.role, payload.role)
    # The new account starts with the role's whole bundle. Without this, an
    # admin whose owner revoked, say, `finance.refund` could mint a fresh
    # admin with a password of their own choosing and have it back.
    _guard_grantable(
        session, permissions_for_role(payload.role) & permissions_for_plan(session.plan)
    )

    problems = validate_password_strength(payload.password)
    if problems:
        raise ValidationError(" ".join(problems))

    username = payload.username.lower()
    email = str(payload.email).strip().lower()
    clash = (
        (
            await db.execute(
                select(User).where(
                    or_(func.lower(User.username) == username, func.lower(User.email) == email)
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
        username=username,
        email=email,
        full_name=payload.full_name,
        password_hash=await hash_password_async(payload.password),
        role=payload.role,
        is_active=True,
        # Somebody other than the account holder chose this password, exactly
        # as with an invite — so it is theirs to replace on first sign-in.
        must_change_password=True,
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
    return _out(row, session.plan, {})


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
    _guard_target(session, row)

    changes = payload.model_dump(exclude_unset=True)
    # `permissions` is not a column; it is handled separately below.
    permissions = changes.pop("permissions", None)
    if changes.get("email") is not None:
        changes["email"] = str(changes["email"]).strip().lower()
    before = {k: getattr(row, k) for k in changes}

    role_changed = "role" in changes and changes["role"] != row.role
    deactivating = changes.get("is_active") is False and row.is_active
    email_changed = "email" in changes and changes["email"] != row.email

    if deactivating and row.id == session.user_id:
        raise ValidationError("You cannot deactivate your own account. Ask another administrator.")

    if email_changed:
        clash = await db.scalar(
            select(func.count())
            .select_from(User)
            .where(func.lower(User.email) == changes["email"], User.id != row.id)
        )
        if clash:
            raise ConflictError("Somebody in this organisation already uses that email.")

    # What they can do today, before anything below changes it — the baseline
    # for "what is this edit granting?" and "what may the editor not touch?".
    current_overrides = (await _overrides_for(db, [row.id])).get(row.id, {})
    current_effective = effective_permissions(row.role, session.plan, current_overrides)

    if role_changed:
        if row.id == session.user_id:
            raise ValidationError("You cannot change your own role. Ask another administrator.")
        _guard_assignable(session.role, changes["role"])
        if permissions is None:
            # The existing overrides carry over onto the new role's bundle, so
            # a promotion hands out whatever that bundle adds. The editor must
            # hold all of it — otherwise promoting someone is a way round an
            # owner's revocation of the editor's own access.
            _guard_grantable(
                session,
                effective_permissions(changes["role"], session.plan, current_overrides)
                - current_effective,
            )

    # The tenant must keep at least one active owner who can administer it.
    losing_owner = row.role is Role.OWNER and (
        deactivating or (role_changed and changes["role"] is not Role.OWNER)
    )
    if losing_owner and await _active_owner_count(db) <= 1:
        raise ConflictError("This is the last active owner. Promote someone else first.")

    if permissions is not None and row.id == session.user_id:
        # Same reasoning as the role guard above: `users.manage` must not be a
        # one-step path to granting yourself everything else.
        raise ValidationError("You cannot change your own access. Ask another administrator.")
    if permissions is not None:
        _guard_assignable(session.role, changes.get("role", row.role))

    for field, value in changes.items():
        setattr(row, field, value)
    await db.flush()

    if permissions is not None:
        requested = _requested_permissions(permissions, session.plan)
        held = set(session.permissions)
        # Only what the editor does NOT already see on this person is a grant.
        _guard_grantable(session, requested - current_effective)
        # And what the editor does not hold, they can neither grant nor take
        # away: those boxes are disabled on their form and never submitted, so
        # reading their absence as "revoke" would silently strip, say, an
        # owner-granted `tenant.billing` every time an admin saves.
        requested = (requested & held) | (set(current_effective) - held)
        await _write_overrides(db, row, requested, plan=session.plan)

    # A demotion, a deactivation, an access change or a new sign-in address
    # must bite immediately, not at next login — the session carries a
    # permission set built at resolve time, and leaving it alone would let the
    # old one stand for up to the idle timeout.
    if role_changed or deactivating or email_changed or permissions is not None:
        await revoke_all_for_user(db, row.id)

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="user.updated",
        entity_type="user",
        entity_id=row.id,
        before=before,
        after={**changes, **({"permissions": sorted(permissions)} if permissions else {})},
    )
    overrides = (await _overrides_for(db, [row.id])).get(row.id, {})
    suliko_account = (await _invite_status_for(db, [row.id])).get(row.id)
    return _out(row, session.plan, overrides, suliko_account)


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
    # Setting someone's password IS signing in as them, so this is the
    # takeover path the rank guard exists for.
    _guard_target(session, row)

    problems = validate_password_strength(payload.password)
    if problems:
        raise ValidationError(" ".join(problems))

    row.password_hash = await hash_password_async(payload.password)
    # Somebody else chose it, so somebody else knows it. Same flag the invite
    # sets: they can sign in, and the only thing they can do is replace it.
    row.must_change_password = True
    await db.flush()
    await revoke_all_for_user(db, row.id)

    from suliko.core.audit import record

    # The password itself is never logged — core.crypto.redact would strip it,
    # but it is simply not passed in the first place.
    await record(db, session, action="user.password_reset", entity_type="user", entity_id=row.id)


@router.delete("/{user_id}/mfa", status_code=http_status.HTTP_204_NO_CONTENT)
async def reset_user_mfa(
    user_id: int,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.USERS_MANAGE))],
) -> None:
    """Remove someone's second factor — a lost or replaced phone.

    They enrol again at their next sign-in (and are made to, when their role
    requires it). The rank guard applies: this is as good as a password reset
    for an account protected by both, so nobody may do it to a senior.
    """
    row = await db.get(User, user_id)
    if row is None:
        raise NotFoundError("User not found.")
    if row.id == session.user_id:
        raise ValidationError("Use your own Account screen to change your two-factor settings.")
    _guard_target(session, row)

    from sqlalchemy import delete

    from suliko.models.user import MfaMethod, MfaRecoveryCode

    await db.execute(delete(MfaMethod).where(MfaMethod.user_id == row.id))
    await db.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == row.id))
    await revoke_all_for_user(db, row.id)

    from suliko.core.audit import record

    await record(db, session, action="user.mfa_reset", entity_type="user", entity_id=row.id)


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
    _guard_target(session, row)

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
