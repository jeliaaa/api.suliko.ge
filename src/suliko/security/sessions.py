"""Opaque server-side sessions.

Chosen over JWTs deliberately. The Next.js BFF holds the token in an HttpOnly
cookie and presents it to this API as a bearer token; nothing reaches the
browser's JavaScript. Revocation is a single UPDATE, which matters when a
role changes or someone is offboarded — a JWT would stay valid until expiry
unless we maintained a deny-list, at which point we have rebuilt sessions with
extra steps.

Reference: docs/03-SECURITY-AND-TENANCY.md §4.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.config import get_settings
from suliko.db.tenancy import bypass_tenant_scope
from suliko.domain.plans import TenantPlan, effective_permissions, effective_plan, parse
from suliko.models.tenant import DEFAULT_TIMEZONE, Tenant
from suliko.models.user import MfaMethod, Role, User, UserPermissionOverride, UserSession
from suliko.security.passwords import generate_token, hash_token
from suliko.security.permissions import Permission

SESSION_TOKEN_PREFIX = "sk_"  # noqa: S105 — a prefix, not a secret


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """Everything a request handler needs about the caller.

    ``tenant_id`` here is the authoritative one, resolved from the stored
    session. Nothing downstream may take a tenant from anywhere else.
    """

    session_id: int
    user_id: int
    username: str
    full_name: str
    email: str
    role: Role
    tenant_id: int
    #: The bureau's slug and display name. Resolved here rather than fetched
    #: per-page because the app shell shows the organisation on every screen,
    #: and the session lookup is already joining this row.
    tenant_slug: str
    tenant_name: str
    #: The plan being ENFORCED. Never null: a tenant that has not chosen yet
    #: is enforced as the default, which is the narrower of the two.
    plan: TenantPlan
    #: True while `tenants.plan` is still null — the tenant signed up and has
    #: not been through onboarding. The frontend routes on this.
    onboarding_required: bool
    #: The password was issued by someone else — an invite, or an admin reset.
    #: `get_authenticated_session` refuses everything while this holds.
    must_change_password: bool
    #: Whether this user has a CONFIRMED second factor.
    #:
    #: Not "did they use it for this session" — whether they could answer a
    #: challenge at all. `can_step_up` below is the only reader, and it is the
    #: difference between step-up being a control and being a dead end.
    has_mfa: bool
    #: Role AND plan, already intersected. Handlers and the sidebar both read
    #: this, so neither has to know that plans exist.
    permissions: frozenset[Permission]
    mfa_satisfied_at: datetime | None
    impersonated_by_user_id: int | None
    #: The bureau's IANA zone, for every "today" question — see `domain.clock`.
    #: Defaulted so a session built anywhere else (tests, tooling) still has a
    #: sane answer.
    timezone: str = DEFAULT_TIMEZONE
    #: The bureau's default UI locale — for links built into its emails.
    tenant_locale: str = "ka"

    @property
    def is_impersonated(self) -> bool:
        return self.impersonated_by_user_id is not None

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    @property
    def can_step_up(self) -> bool:
        """Whether demanding a fresh code is a thing this user can act on.

        Step-up exists so a stolen session cannot immediately move money. It
        can only do that if there is a factor to re-present. With MFA off, or
        with no factor enrolled, demanding one does not raise the bar — it
        just refuses the action permanently, because nothing the user can do
        will satisfy it.
        """
        return get_settings().mfa_enforced and self.has_mfa

    def mfa_age_seconds(self, now: datetime | None = None) -> float | None:
        if self.mfa_satisfied_at is None:
            return None
        return ((now or datetime.now(UTC)) - self.mfa_satisfied_at).total_seconds()


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Returned once at login. ``token`` is never recoverable afterwards."""

    token: str
    session: UserSession


async def create_session(
    db: AsyncSession,
    user: User,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    mfa_satisfied: bool = False,
    impersonated_by_user_id: int | None = None,
    impersonation_reason: str | None = None,
) -> IssuedSession:
    settings = get_settings()
    now = datetime.now(UTC)

    token = generate_token(SESSION_TOKEN_PREFIX)

    session = UserSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        token_hash=hash_token(token),
        last_seen_at=now,
        idle_expires_at=now + timedelta(minutes=settings.session_idle_timeout_minutes),
        absolute_expires_at=now + timedelta(hours=settings.session_absolute_timeout_hours),
        mfa_satisfied_at=now if mfa_satisfied else None,
        ip=ip,
        user_agent=(user_agent or "")[:255] or None,
        impersonated_by_user_id=impersonated_by_user_id,
        impersonation_reason=impersonation_reason,
    )
    db.add(session)
    await db.flush()

    return IssuedSession(token=token, session=session)


async def resolve_session(db: AsyncSession, token: str) -> AuthenticatedSession | None:
    """Look up a session token and slide its idle window.

    Runs under ``bypass_tenant_scope`` for one reason: we do not yet know the
    tenant — that is the question being answered. The lookup is by a 256-bit
    token hash, so there is nothing to enumerate, and the tenant is bound
    immediately afterwards by the caller.
    """
    if not token:
        return None

    now = datetime.now(UTC)
    token_digest = hash_token(token)

    with bypass_tenant_scope():
        row = (
            await db.execute(
                select(UserSession, User, Tenant)
                .join(User, User.id == UserSession.user_id)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(UserSession.token_hash == token_digest)
            )
        ).first()

        if row is None:
            return None

        user_session, user, tenant = row

        if not user_session.is_valid_at(now):
            return None
        if not user.is_active:
            return None

        # Suspending a tenant has to take effect now, not whenever their
        # sessions happen to expire. Login already refuses a suspended tenant;
        # without the same check here, suspending a bureau left every signed-in
        # user of it working for up to the absolute session timeout.
        #
        # The tenant row is already joined for its slug and display name, so
        # this costs nothing.
        if not tenant.is_usable:
            return None

        # Bulk revocation: a password or role change stamps
        # ``sessions_invalid_before``, retiring every session issued earlier
        # without having to delete them one by one.
        if (
            user.sessions_invalid_before is not None
            and user_session.created_at < user.sessions_invalid_before
        ):
            return None

        plan = effective_plan(tenant.plan)

        has_mfa = bool(
            await db.scalar(
                select(MfaMethod.id)
                .where(
                    MfaMethod.user_id == user.id,
                    MfaMethod.confirmed_at.is_not(None),
                )
                .limit(1)
            )
        )

        # Per-user grants and revocations. A second query rather than a join:
        # this is 0..N rows per user and joining would multiply the session
        # row, which is the one thing this lookup must return exactly one of.
        overrides = {
            row.permission: row.granted
            for row in (
                await db.execute(
                    select(UserPermissionOverride).where(UserPermissionOverride.user_id == user.id)
                )
            )
            .scalars()
            .all()
        }

        settings = get_settings()
        user_session.last_seen_at = now
        user_session.idle_expires_at = now + timedelta(
            minutes=settings.session_idle_timeout_minutes
        )

        return AuthenticatedSession(
            session_id=user_session.id,
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            email=user.email,
            role=user.role,
            tenant_id=user.tenant_id,
            tenant_slug=tenant.slug,
            tenant_name=tenant.display_name,
            plan=plan,
            onboarding_required=parse(tenant.plan) is None,
            must_change_password=user.must_change_password,
            has_mfa=has_mfa,
            # The single place a plan turns into a refusal. Every `require()`
            # in the API and every nav item in the sidebar reads the result,
            # so no handler needs its own plan check.
            permissions=effective_permissions(user.role, plan, overrides),
            mfa_satisfied_at=user_session.mfa_satisfied_at,
            impersonated_by_user_id=user_session.impersonated_by_user_id,
            timezone=tenant.timezone or DEFAULT_TIMEZONE,
            tenant_locale=tenant.locale or "ka",
        )


async def mark_mfa_satisfied(db: AsyncSession, session_id: int) -> None:
    with bypass_tenant_scope():
        user_session = await db.get(UserSession, session_id)
        if user_session is not None:
            user_session.mfa_satisfied_at = datetime.now(UTC)


async def revoke_session(db: AsyncSession, session_id: int) -> None:
    with bypass_tenant_scope():
        user_session = await db.get(UserSession, session_id)
        if user_session is not None and user_session.revoked_at is None:
            user_session.revoked_at = datetime.now(UTC)


async def revoke_all_for_user(db: AsyncSession, user_id: int) -> None:
    """Used on password change, role change and offboarding."""
    with bypass_tenant_scope():
        user = await db.get(User, user_id)
        if user is not None:
            user.sessions_invalid_before = datetime.now(UTC)
