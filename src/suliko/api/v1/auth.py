"""Authentication: login, the 2FA challenge, step-up, logout.

The login flow is two-legged by design:

    POST /auth/login      password -> a session, possibly with MFA pending
    POST /auth/mfa/verify TOTP code -> that same session, MFA satisfied

A session with MFA pending can reach nothing except the second call. That is
enforced by ``get_authenticated_session``, which every other route depends on.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import (
    Db,
    get_client_ip,
    get_current_session,
    get_session_for_password_change,
)
from suliko.config import get_settings
from suliko.core import mail
from suliko.core.crypto import decrypt_for_tenant
from suliko.core.errors import (
    AuthenticationError,
    ConflictError,
    RateLimitedError,
    ValidationError,
)
from suliko.core.ratelimit import RateLimiter, get_rate_limiter
from suliko.db.session import bind_tenant_guc, get_sessionmaker, session_scope
from suliko.db.tenancy import bypass_tenant_scope, tenant_scope
from suliko.domain.plans import effective_permissions, effective_plan
from suliko.domain.reference_seed import seed_reference_data
from suliko.models.reference import TenantSettings
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import LoginAttempt, MfaMethod, MfaRecoveryCode, Role, User
from suliko.security import reset_tokens
from suliko.security import totp as totp_service
from suliko.security.passwords import (
    hash_password,
    hash_token,
    validate_password_strength,
    verify_and_maybe_rehash,
    waste_time_verifying,
)
from suliko.security.permissions import requires_mfa
from suliko.security.sessions import (
    AuthenticatedSession,
    create_session,
    mark_mfa_satisfied,
    resolve_session,
    revoke_all_for_user,
    revoke_session,
)

log = structlog.get_logger()
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)
    #: Which bureau to sign in to. A username is unique per tenant, not
    #: globally, so this disambiguates. It selects a candidate — it does not
    #: grant anything; the tenant is re-derived from the user row.
    tenant_slug: str = Field(min_length=1, max_length=63)


class LoginResponse(BaseModel):
    session_token: str
    mfa_required: bool
    #: True when the user must enrol a second factor before proceeding.
    mfa_enrolment_required: bool
    #: Signed in on a password somebody else chose. The frontend sends them
    #: straight to the change-password screen.
    must_change_password: bool = False
    user_id: int
    tenant_id: int
    role: str
    permissions: list[str]


class MfaVerifyRequest(BaseModel):
    code: str = Field(min_length=6, max_length=11)


class MfaVerifyResponse(BaseModel):
    ok: bool
    permissions: list[str]


async def _record_attempt(
    db: AsyncSession,
    username: str,
    ip: str | None,
    user_agent: str | None,
    succeeded: bool,
    reason: str | None = None,
) -> None:
    with bypass_tenant_scope():
        db.add(
            LoginAttempt(
                username=username[:100],
                ip=ip,
                user_agent=(user_agent or "")[:255] or None,
                succeeded=succeeded,
                failure_reason=reason,
            )
        )


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> LoginResponse:
    """Authenticate with a password.

    Failure is uniform: the same message, the same status, and — via
    ``waste_time_verifying`` — approximately the same latency whether the
    tenant, the user, or the password was wrong. Anything else enumerates
    accounts.
    """
    ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent")

    # Per-account and per-IP, so one attacker cannot lock a real user out
    # platform-wide by hammering their username from everywhere.
    account_key = f"login:acct:{payload.tenant_slug}:{payload.username.lower()}"
    ip_key = f"login:ip:{ip or 'unknown'}"

    if retry := await limiter.check_login(account_key, ip_key):
        raise RateLimitedError("Too many attempts. Try again later.", retry_after=retry)

    async with get_sessionmaker()() as db:
        with bypass_tenant_scope():
            tenant = (
                await db.execute(select(Tenant).where(Tenant.slug == payload.tenant_slug))
            ).scalar_one_or_none()

            user: User | None = None
            if tenant is not None and tenant.is_usable:
                user = (
                    await db.execute(
                        select(User).where(
                            User.tenant_id == tenant.id,
                            User.username == payload.username,
                        )
                    )
                ).scalar_one_or_none()

        if user is None or not user.is_active:
            waste_time_verifying()
            await limiter.record_login_failure(account_key, ip_key)
            await _record_attempt(db, payload.username, ip, user_agent, False, "no_such_user")
            await db.commit()
            raise AuthenticationError("Invalid username or password.")

        ok, new_hash = verify_and_maybe_rehash(payload.password, user.password_hash)
        if not ok:
            await limiter.record_login_failure(account_key, ip_key)
            await _record_attempt(db, payload.username, ip, user_agent, False, "bad_password")
            await db.commit()
            raise AuthenticationError("Invalid username or password.")

        # Transparent bcrypt -> Argon2id upgrade, on the user's own login.
        if new_hash is not None:
            with bypass_tenant_scope():
                user.password_hash = new_hash
            log.info("password_rehashed", user_id=user.id, tenant_id=user.tenant_id)

        # This session was opened before we knew which tenant we were acting
        # for, so it carries no RLS GUC. Everything below writes tenant-scoped
        # rows — set it now, or the `tenant_isolation` policy rejects them the
        # moment the app stops connecting as a PostgreSQL superuser.
        await bind_tenant_guc(db, user.tenant_id)

        with tenant_scope(user.tenant_id):
            mfa = (
                (
                    await db.execute(
                        select(MfaMethod).where(
                            MfaMethod.user_id == user.id,
                            MfaMethod.confirmed_at.is_not(None),
                        )
                    )
                )
                .scalars()
                .first()
            )

            settings = get_settings()
            enforced = settings.mfa_enforced
            require_enrolment = settings.mfa_require_enrolment
            has_mfa = mfa is not None
            must_have_mfa = enforced and requires_mfa(user.role)

            # A challenge is owed when a factor exists and MFA is switched on.
            # Note this covers VOLUNTARY enrolment: a staff user who added
            # TOTP is challenged even though their role does not demand it.
            challenge_owed = enforced and has_mfa

            # The role demands a factor and none is enrolled.
            #
            # Failing closed here is the stronger policy, and it is gated
            # behind MFA_REQUIRE_ENROLMENT because it is only honest once a
            # user can enrol for themselves. Until then it does not prompt
            # anyone to add a factor — enrolment is a command on the server —
            # it just locks the owner and every admin of every tenant out of
            # their own product. See Settings.mfa_require_enrolment.
            enrolment_required = require_enrolment and must_have_mfa and not has_mfa

            if must_have_mfa and not has_mfa and not require_enrolment:
                # Logged per login, not per boot: this is the record of WHICH
                # privileged accounts are running without a second factor, and
                # it is the list to work through when enrolment ships.
                log.warning(
                    "privileged_login_without_mfa",
                    user_id=user.id,
                    tenant_id=user.tenant_id,
                    tenant_slug=tenant.slug if tenant else None,
                    role=user.role.value,
                )

            issued = await create_session(
                db,
                user,
                ip=ip,
                user_agent=user_agent,
                mfa_satisfied=not (challenge_owed or enrolment_required),
            )

            user.last_login_at = datetime.now(UTC)
            await _record_attempt(db, payload.username, ip, user_agent, True)

        await limiter.clear_login_failures(account_key)
        await db.commit()

        response.status_code = status.HTTP_200_OK
        return LoginResponse(
            session_token=issued.token,
            # "Go to the challenge screen now" — NOT "a factor exists". With
            # MFA switched off this is false even for a user who has TOTP
            # enrolled, so the frontend sends them straight to the dashboard.
            mfa_required=challenge_owed,
            mfa_enrolment_required=enrolment_required,
            must_change_password=user.must_change_password,
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role.value,
            # Masked by the plan, exactly as resolve_session does. Reporting
            # the raw role bundle here would have the frontend paint tabs
            # that every request behind them then refuses.
            permissions=sorted(
                p.value for p in effective_permissions(user.role, effective_plan(tenant.plan))
            ),
        )


@router.post("/mfa/verify", response_model=MfaVerifyResponse)
async def verify_mfa(
    payload: MfaVerifyRequest,
    request: Request,
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> MfaVerifyResponse:
    """Complete the second factor, or spend a recovery code.

    Depends on ``get_current_session`` rather than ``get_authenticated_session``
    — this is the one endpoint an MFA-pending session is allowed to call.
    """
    key = f"mfa:{session.user_id}"
    if retry := await limiter.check_mfa(key):
        raise RateLimitedError("Too many codes. Try again shortly.", retry_after=retry)

    code = payload.code.strip()

    async with session_scope() as db:
        method = (
            (
                await db.execute(
                    select(MfaMethod).where(
                        MfaMethod.user_id == session.user_id,
                        MfaMethod.confirmed_at.is_not(None),
                    )
                )
            )
            .scalars()
            .first()
        )

        if method is None:
            raise ValidationError("No second factor is enrolled for this account.")

        # A recovery code, not a TOTP code.
        if "-" in code:
            normalised = totp_service.normalise_recovery_code(code)
            digest = hash_token(normalised)
            recovery = (
                (
                    await db.execute(
                        select(MfaRecoveryCode).where(
                            MfaRecoveryCode.user_id == session.user_id,
                            MfaRecoveryCode.code_hash == digest,
                            MfaRecoveryCode.used_at.is_(None),
                        )
                    )
                )
                .scalars()
                .first()
            )

            if recovery is None:
                await limiter.record_mfa_failure(key)
                raise AuthenticationError("Invalid code.")

            recovery.used_at = datetime.now(UTC)
            await mark_mfa_satisfied(db, session.session_id)
            log.info("mfa_recovery_code_used", user_id=session.user_id)
            return MfaVerifyResponse(
                ok=True, permissions=sorted(p.value for p in session.permissions)
            )

        secret = decrypt_for_tenant(session.tenant_id, method.secret_encrypted)

        try:
            result = totp_service.verify_code(
                secret, code, last_used_timestep=method.last_used_timestep
            )
        except totp_service.ReplayError:
            # The code was arithmetically valid but its step is spent. That is
            # not a typo — it means someone is replaying a code the real user
            # already used, so it is logged at warning.
            await limiter.record_mfa_failure(key)
            log.warning("mfa_replay_rejected", user_id=session.user_id)
            raise AuthenticationError("Invalid code.") from None

        if not result.ok:
            await limiter.record_mfa_failure(key)
            raise AuthenticationError("Invalid code.")

        method.last_used_timestep = result.timestep
        await mark_mfa_satisfied(db, session.session_id)
        await limiter.clear_mfa_failures(key)

    return MfaVerifyResponse(ok=True, permissions=sorted(p.value for p in session.permissions))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> None:
    async with session_scope() as db:
        await revoke_session(db, session.session_id)


# ── Signing up ──────────────────────────────────────────────────────────────
#
# Creates a TENANT, not just a user. That is the part worth pausing on: this
# is the one unauthenticated endpoint that writes a new row to `tenants`, so
# it is rate limited hard by IP and it is the only place outside the CLI
# allowed to do it.
#
# The new tenant's plan is left NULL, which means "has not chosen yet". The
# session says `onboarding_required` and the frontend sends them to the
# onboarding screen; until they choose, they are enforced as a freelancer.


class SignupRequest(BaseModel):
    organisation_name: str = Field(min_length=2, max_length=255)
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)


class SignupResponse(BaseModel):
    session_token: str
    tenant_slug: str
    #: What to type in the Organisation and Username boxes next time. Returned
    #: because both are derived rather than chosen, and a credential the user
    #: cannot reproduce is one they cannot come back to.
    username: str
    onboarding_required: bool = True


def _slug_candidate(name: str) -> str:
    """A URL-safe slug from an organisation name.

    Georgian is transliterated to nothing useful by any cheap scheme, so a
    name with no ASCII letters falls back to a generic stem plus the uniqueness
    suffix below — `bureau-4` is a worse handle than `tbilisi-translations`,
    but it is one the owner can read back over the phone, which a percent-
    encoded Mkhedruli slug is not.
    """
    ascii_only = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:40].strip("-")
    # The slug column demands 2-63 chars starting alphanumeric.
    return slug if len(slug) >= 2 else "bureau"


async def _unique_slug(db: AsyncSession, name: str) -> str:
    """The candidate, or the first free `-N` suffix after it."""
    base = _slug_candidate(name)

    with bypass_tenant_scope():
        taken = set(
            (
                await db.execute(
                    select(Tenant.slug).where(
                        or_(Tenant.slug == base, Tenant.slug.like(f"{base}-%"))
                    )
                )
            )
            .scalars()
            .all()
        )

    if base not in taken:
        return base
    # Bounded: a name colliding 999 times is abuse, not a naming coincidence.
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"
        if candidate not in taken:
            return candidate
    raise ConflictError("Could not allocate an organisation handle. Try a different name.")


@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: SignupRequest,
    request: Request,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> SignupResponse:
    """Create a new bureau and its owner, and sign them in.

    The account is an OWNER of its own brand-new tenant, which is not a
    privilege escalation: the tenant contains nothing but them. What the plan
    withholds — employees, payroll, the other integrations — is decided at
    onboarding and enforced by `domain/plans.py`, not by the role.

    The username is the email address. Both are unique per tenant and the
    tenant is empty, so neither can collide; and it removes the one question a
    derived username always raises, which is what to type at the login screen.
    """
    ip = get_client_ip(request)
    settings = get_settings()

    ip_key = f"signup:ip:{ip or 'unknown'}"
    if retry := await limiter.check_signup(ip_key):
        raise RateLimitedError("Too many sign-ups from this address.", retry_after=retry)

    problems = validate_password_strength(payload.password)
    if problems:
        raise ValidationError(" ".join(problems))

    email = str(payload.email).strip().lower()
    # `users.username` is String(100); an address longer than that keeps its
    # local part, which is still unique inside a tenant of one.
    username = email if len(email) <= 100 else email.split("@")[0][:100]

    await limiter.record_signup(ip_key)

    async with get_sessionmaker()() as db:
        slug = await _unique_slug(db, payload.organisation_name)

        with bypass_tenant_scope():
            tenant = Tenant(
                slug=slug,
                display_name=payload.organisation_name.strip(),
                status=TenantStatus.TRIAL,
                # NULL: "signed up, has not chosen a plan". Onboarding sets it.
                plan=None,
                locale=settings.default_signup_locale,
            )
            db.add(tenant)
            await db.flush()

        tenant_id = int(tenant.id)

        # Every row below is tenant-scoped, and this session was opened before
        # the tenant existed at all — so nothing has set the RLS GUC.
        await bind_tenant_guc(db, tenant_id)

        with tenant_scope(tenant_id):
            db.add(TenantSettings(tenant_id=tenant_id, default_language=tenant.locale))
            # The same starter catalogues `suliko seed-reference` adds, minus
            # prices — see domain/reference_seed.py for why. Without these the
            # very first "New translation" has no document type to choose.
            await seed_reference_data(db)
            user = User(
                tenant_id=tenant_id,
                username=username,
                email=email,
                full_name=payload.full_name.strip(),
                password_hash=hash_password(payload.password),
                role=Role.OWNER,
                is_active=True,
            )
            db.add(user)
            await db.flush()

            # Signed straight in. Sending someone who just chose a password
            # back to a login form to retype it is friction with no security
            # benefit — they proved possession of the password by setting it.
            issued = await create_session(
                db,
                user,
                ip=ip,
                user_agent=request.headers.get("user-agent"),
                # An owner is in MFA_REQUIRED_ROLES. With MFA_REQUIRE_ENROLMENT
                # off (the default) there is no factor to owe yet; with it on,
                # this session is unsatisfied and the frontend says so rather
                # than silently handing out a privileged session.
                mfa_satisfied=not (
                    settings.mfa_enforced
                    and settings.mfa_require_enrolment
                    and requires_mfa(Role.OWNER)
                ),
            )
            user.last_login_at = datetime.now(UTC)

        from suliko.core.audit import record

        await record(
            db,
            None,
            action="tenant.signed_up",
            entity_type="tenant",
            entity_id=tenant_id,
            tenant_id=tenant_id,
            ip=ip,
            after={"slug": slug, "owner_email": email},
        )
        await db.commit()

    log.info("tenant_signed_up", tenant_id=tenant_id, slug=slug)

    return SignupResponse(
        session_token=issued.token,
        tenant_slug=slug,
        username=username,
    )


# ── Passwords ───────────────────────────────────────────────────────────────
#
# Three endpoints, two of them unauthenticated:
#
#   POST /auth/password/forgot   email a single-use link      (anonymous)
#   POST /auth/password/reset    spend that link              (anonymous)
#   POST /auth/password/change   with the current password    (signed in)
#
# All three revoke every session the user has. On a reset that is the whole
# point — if the account was taken over, leaving the attacker's session alive
# defeats the recovery. On a deliberate change it is the same reasoning one
# step weaker, and it matches what `users.reset_password` already does when an
# admin sets someone's password for them.


class ForgotPasswordRequest(BaseModel):
    tenant_slug: str = Field(min_length=1, max_length=63)
    #: Username or email. People remember one or the other, rarely both, and
    #: accepting either costs nothing because the answer is the same regardless.
    identifier: str = Field(min_length=1, max_length=255)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


def _reset_email(user: User, tenant: Tenant, link: str, ttl_minutes: int) -> tuple[str, str]:
    """Subject and plain-text body. No HTML — see core/mail.py."""
    hours = ttl_minutes // 60
    validity = f"{hours} hour{'s' if hours != 1 else ''}" if hours else f"{ttl_minutes} minutes"
    body = (
        f"Hello {user.full_name or user.username},\n\n"
        f"Someone asked to reset the password for your Suliko account "
        f"({user.username}) at {tenant.display_name}.\n\n"
        f"Open this link to choose a new one:\n\n{link}\n\n"
        f"The link works once and expires in {validity}.\n\n"
        f"If this wasn't you, you can ignore this email — your password has "
        f"not changed. Nobody can use this link without opening it.\n"
    )
    return "Reset your Suliko password", body


@router.post("/password/forgot", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    """Send a reset link, if there is anywhere to send it.

    Answers 204 whether or not the account exists, and does the same amount of
    work either way. Anything else — a different status, a different message,
    a visibly different latency — turns this into a free tool for discovering
    which addresses are registered with which bureau.

    The 429 is the one exception, and it leaks nothing: it is keyed on the
    identifier the caller just typed, which they already know.
    """
    ip = get_client_ip(request)
    identifier = payload.identifier.strip()

    account_key = f"pwreset:acct:{payload.tenant_slug}:{identifier.lower()}"
    ip_key = f"pwreset:ip:{ip or 'unknown'}"

    if retry := await limiter.check_password_reset(account_key, ip_key):
        raise RateLimitedError("Too many reset requests. Try again later.", retry_after=retry)
    await limiter.record_password_reset_request(account_key, ip_key)

    settings = get_settings()

    async with get_sessionmaker()() as db:
        with bypass_tenant_scope():
            tenant = (
                await db.execute(select(Tenant).where(Tenant.slug == payload.tenant_slug))
            ).scalar_one_or_none()

            user: User | None = None
            if tenant is not None and tenant.is_usable:
                user = (
                    (
                        await db.execute(
                            select(User).where(
                                User.tenant_id == tenant.id,
                                or_(
                                    User.username == identifier,
                                    func.lower(User.email) == identifier.lower(),
                                ),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )

        if user is None or not user.is_active or tenant is None:
            # Deliberately silent. Logged so an operator can see that someone
            # is trying, without the caller learning anything.
            log.info(
                "password_reset_requested_for_unknown_account",
                tenant_slug=payload.tenant_slug,
                ip=ip,
            )
            return

        token = await reset_tokens.issue(
            db, user, ttl_seconds=settings.password_reset_ttl_minutes * 60
        )

        from suliko.core.audit import record

        await record(
            db,
            None,
            action="user.password_reset_requested",
            entity_type="user",
            entity_id=user.id,
            tenant_id=user.tenant_id,
            ip=ip,
        )
        await db.commit()

    # After the commit: a link that reaches the user before its row is durable
    # is a link that does not work.
    link = (
        f"{settings.app_url.rstrip('/')}/{tenant.locale}/reset-password"
        f"?token={quote(token, safe='')}"
    )
    subject, body = _reset_email(user, tenant, link, settings.password_reset_ttl_minutes)
    await mail.send(user.email, subject, body)


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(payload: ResetPasswordRequest, request: Request) -> None:
    """Spend a reset link and set the new password.

    Strength is checked BEFORE the token is spent. Rejecting a too-short
    password and burning the link in the same breath would send the user back
    to their inbox for a second email over a typo — the token is single-use
    against a successful reset, not against a failed attempt at one.
    """
    problems = validate_password_strength(payload.password)
    if problems:
        raise ValidationError(" ".join(problems))

    async with get_sessionmaker()() as db:
        user = await reset_tokens.consume(db, payload.token)
        if user is None:
            # One message for expired, spent, forged and unknown alike.
            raise ValidationError(
                "This reset link is no longer valid. Request a new one and "
                "use the most recent email."
            )

        with bypass_tenant_scope():
            user.password_hash = hash_password(payload.password)
        await db.flush()
        await revoke_all_for_user(db, user.id)

        from suliko.core.audit import record

        await record(
            db,
            None,
            action="user.password_reset_completed",
            entity_type="user",
            entity_id=user.id,
            tenant_id=user.tenant_id,
            ip=get_client_ip(request),
        )
        await db.commit()

    log.info("password_reset_completed", user_id=user.id, tenant_id=user.tenant_id)


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: ChangePasswordRequest,
    session: Annotated[AuthenticatedSession, Depends(get_session_for_password_change)],
    db: Db,
) -> None:
    """Change your own password, proving you know the current one.

    Deliberately NOT behind `get_authenticated_session`. Someone holding a
    one-time password from an invite is refused by that gate everywhere else,
    and this is the screen they are being sent to — gating it the same way
    would leave them with a session that can do nothing at all.

    No step-up 2FA: the current password IS the proof, and demanding a TOTP
    code as well would stop exactly the people who most need to rotate a
    password they think has leaked.

    Every session is revoked, including the one making this call — so the
    caller is signed out and has to sign in again. That is the honest
    behaviour: "changed everywhere" is what a user believes has happened, and
    quietly keeping one session alive makes that belief wrong.
    """
    user = await db.get(User, session.user_id)
    if user is None:
        raise AuthenticationError("Your account is no longer available.")

    ok, _ = verify_and_maybe_rehash(payload.current_password, user.password_hash)
    if not ok:
        # 422 rather than 401: the session is perfectly valid, one field is
        # wrong. A 401 here would log the user out of the UI mid-form.
        raise ValidationError("Your current password is not correct.")

    if payload.new_password == payload.current_password:
        raise ValidationError("The new password must be different from the current one.")

    problems = validate_password_strength(payload.new_password)
    if problems:
        raise ValidationError(" ".join(problems))

    user.password_hash = hash_password(payload.new_password)
    # Whatever they were handed, they have now replaced. This is the only
    # place the flag is cleared — an admin resetting someone's password sets
    # it again, which is the point.
    user.must_change_password = False
    await db.flush()
    await revoke_all_for_user(db, user.id)

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="user.password_changed",
        entity_type="user",
        entity_id=user.id,
    )


class SessionInfo(BaseModel):
    user_id: int
    username: str
    full_name: str
    email: str
    role: str
    tenant_id: int
    tenant_slug: str
    tenant_name: str
    #: The plan being enforced, and whether it was actually chosen. The shell
    #: routes on `onboarding_required`; the sidebar gates the permission-less
    #: tabs on `plan`.
    plan: str
    onboarding_required: bool
    #: The password came from an invite or an admin reset. Every endpoint
    #: behind `require()` refuses until it is replaced.
    must_change_password: bool
    permissions: list[str]
    mfa_satisfied: bool
    is_impersonated: bool


@router.get("/session", response_model=SessionInfo)
async def current_session(
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> SessionInfo:
    """What the BFF calls on every page load to hydrate the shell."""
    return SessionInfo(
        user_id=session.user_id,
        username=session.username,
        full_name=session.full_name,
        email=session.email,
        role=session.role.value,
        tenant_id=session.tenant_id,
        tenant_slug=session.tenant_slug,
        tenant_name=session.tenant_name,
        plan=session.plan.value,
        onboarding_required=session.onboarding_required,
        must_change_password=session.must_change_password,
        permissions=sorted(p.value for p in session.permissions),
        # Must agree with the gate in deps.get_authenticated_session. When MFA
        # is switched off the gate lets every request through, so reporting
        # "not satisfied" here would strand the frontend on a challenge screen
        # for a requirement the server is no longer enforcing — which is
        # exactly what happens to sessions created BEFORE the switch, whose
        # mfa_satisfied_at is null.
        mfa_satisfied=(session.mfa_satisfied_at is not None or not get_settings().mfa_enforced),
        is_impersonated=session.is_impersonated,
    )


__all__ = ["resolve_session", "router"]
