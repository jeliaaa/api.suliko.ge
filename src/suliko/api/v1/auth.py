"""Authentication: login, the 2FA challenge, step-up, logout.

The login flow is two-legged by design:

    POST /auth/login      password -> a session, possibly with MFA pending
    POST /auth/mfa/verify TOTP code -> that same session, MFA satisfied

A session with MFA pending can reach nothing except the second call. That is
enforced by ``get_authenticated_session``, which every other route depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import get_client_ip, get_current_session
from suliko.core.crypto import decrypt_for_tenant
from suliko.core.errors import AuthenticationError, RateLimitedError, ValidationError
from suliko.core.ratelimit import RateLimiter, get_rate_limiter
from suliko.db.session import get_sessionmaker, session_scope
from suliko.db.tenancy import bypass_tenant_scope, tenant_scope
from suliko.models.tenant import Tenant
from suliko.models.user import LoginAttempt, MfaMethod, MfaRecoveryCode, User
from suliko.security import totp as totp_service
from suliko.security.passwords import (
    hash_token,
    verify_and_maybe_rehash,
    waste_time_verifying,
)
from suliko.security.permissions import requires_mfa
from suliko.security.sessions import (
    AuthenticatedSession,
    create_session,
    mark_mfa_satisfied,
    resolve_session,
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

            has_mfa = mfa is not None
            must_have_mfa = requires_mfa(user.role)

            issued = await create_session(
                db,
                user,
                ip=ip,
                user_agent=user_agent,
                # Only a user with no second factor at all, and none required,
                # is fully authenticated by password alone.
                mfa_satisfied=not has_mfa and not must_have_mfa,
            )

            user.last_login_at = datetime.now(UTC)
            await _record_attempt(db, payload.username, ip, user_agent, True)

        await limiter.clear_login_failures(account_key)
        await db.commit()

        from suliko.security.permissions import permissions_for_role

        # A privileged role with no enrolled factor must enrol before it can
        # do anything — it is not let through, it is redirected to enrolment.
        enrolment_required = must_have_mfa and not has_mfa

        response.status_code = status.HTTP_200_OK
        return LoginResponse(
            session_token=issued.token,
            mfa_required=has_mfa,
            mfa_enrolment_required=enrolment_required,
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role.value,
            permissions=sorted(p.value for p in permissions_for_role(user.role)),
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


class SessionInfo(BaseModel):
    user_id: int
    username: str
    full_name: str
    email: str
    role: str
    tenant_id: int
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
        permissions=sorted(p.value for p in session.permissions),
        mfa_satisfied=session.mfa_satisfied_at is not None,
        is_impersonated=session.is_impersonated,
    )


__all__ = ["resolve_session", "router"]
