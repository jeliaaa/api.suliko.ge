"""Two-factor enrolment, from the product rather than the server console.

Until this existed, a second factor could only be added by running a command
on the API server — so no tenant could turn it on, `MFA_REQUIRE_ENROLMENT`
could not be enabled without locking every owner out, and step-up (the fresh
code demanded before users, settings and transfers) was skipped for everyone.

    POST /auth/mfa/enrol          start: a secret, as an otpauth URI and a QR
    POST /auth/mfa/confirm        prove one code -> factor on, recovery codes
    POST /auth/mfa/recovery-codes a fresh set (needs a current code)
    POST /auth/mfa/disable        off again (needs a current code)

`POST /auth/mfa/verify` (in `auth.py`) is both the login challenge and the
step-up: it stamps the session's `mfa_satisfied_at`, which is what step-up's
age check reads.

## Who may enrol

A fully signed-in user, obviously. And ALSO a session that is MFA-pending for
the one reason that it has no factor yet — what login hands out when
`MFA_REQUIRE_ENROLMENT` is on and a privileged account has none. That session
can do nothing else, and without this it could not do this either, which is
the dead end that kept the switch off. A session that is pending because a
factor EXISTS and has not been answered is refused: enrolling a second one
would be a way round the challenge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import qrcode
import qrcode.image.svg
import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import get_current_session
from suliko.config import get_settings
from suliko.core.crypto import decrypt_for_tenant, encrypt_for_tenant
from suliko.core.errors import (
    AuthenticationError,
    ConflictError,
    MfaRequiredError,
    PasswordChangeRequiredError,
    RateLimitedError,
    ValidationError,
)
from suliko.core.ratelimit import RateLimiter, get_rate_limiter
from suliko.db.session import session_scope
from suliko.models.user import MfaMethod, MfaRecoveryCode
from suliko.security import totp as totp_service
from suliko.security.passwords import hash_token
from suliko.security.permissions import requires_mfa
from suliko.security.sessions import AuthenticatedSession, mark_mfa_satisfied

log = structlog.get_logger()
router = APIRouter(prefix="/auth/mfa", tags=["auth"])

ISSUER = "Suliko CRM"


class EnrolmentOut(BaseModel):
    #: For typing into an app by hand. Shown once; never logged.
    secret: str
    #: otpauth:// — contains the secret. Never put it in a URL or a log.
    otpauth_uri: str
    #: The same URI as a scannable SVG, so the frontend needs no QR library.
    qr_svg: str


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=11)


class RecoveryCodesOut(BaseModel):
    #: Shown ONCE. Only their hashes are stored.
    recovery_codes: list[str]


class MfaStatus(BaseModel):
    enabled: bool
    #: Unused recovery codes left.
    recovery_codes_remaining: int
    #: Whether this account may switch it off (its role may require it).
    can_disable: bool


async def _confirmed(db: AsyncSession, user_id: int) -> MfaMethod | None:
    return (
        (
            await db.execute(
                select(MfaMethod).where(
                    MfaMethod.user_id == user_id, MfaMethod.confirmed_at.is_not(None)
                )
            )
        )
        .scalars()
        .first()
    )


def _role_requires_it(session: AuthenticatedSession) -> bool:
    settings = get_settings()
    return settings.mfa_enforced and settings.mfa_require_enrolment and requires_mfa(session.role)


async def _enrolling_session(
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> AuthenticatedSession:
    """Signed in, or MFA-pending ONLY because there is no factor yet."""
    if session.must_change_password:
        raise PasswordChangeRequiredError("Set your own password before continuing.")
    if session.mfa_satisfied_at is None and get_settings().mfa_enforced and session.has_mfa:
        # A factor exists and has not been answered: this would bypass it.
        raise MfaRequiredError("Two-factor authentication is required to continue.")
    return session


async def _signed_in(
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> AuthenticatedSession:
    if session.must_change_password:
        raise PasswordChangeRequiredError("Set your own password before continuing.")
    if session.mfa_satisfied_at is None and get_settings().mfa_enforced:
        raise MfaRequiredError("Two-factor authentication is required to continue.")
    return session


Enrolling = Annotated[AuthenticatedSession, Depends(_enrolling_session)]
SignedIn = Annotated[AuthenticatedSession, Depends(_signed_in)]
Limiter = Annotated[RateLimiter, Depends(get_rate_limiter)]


async def _prove(
    db: AsyncSession,
    session: AuthenticatedSession,
    method: MfaMethod,
    code: str,
    limiter: RateLimiter,
) -> None:
    """A current TOTP code (or an unused recovery code), or refuse.

    The same failure counter as the login challenge, so these endpoints are
    not a second, unthrottled place to guess codes.
    """
    key = f"mfa:{session.user_id}"
    if retry := await limiter.check_mfa(key):
        raise RateLimitedError("Too many codes. Try again shortly.", retry_after=retry)

    code = code.strip()
    if "-" in code:
        recovery = (
            (
                await db.execute(
                    select(MfaRecoveryCode).where(
                        MfaRecoveryCode.user_id == session.user_id,
                        MfaRecoveryCode.code_hash
                        == hash_token(totp_service.normalise_recovery_code(code)),
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
        return

    secret = decrypt_for_tenant(session.tenant_id, method.secret_encrypted)
    try:
        result = totp_service.verify_code(
            secret, code, last_used_timestep=method.last_used_timestep
        )
    except totp_service.ReplayError:
        await limiter.record_mfa_failure(key)
        raise AuthenticationError("Invalid code.") from None
    if not result.ok:
        await limiter.record_mfa_failure(key)
        raise AuthenticationError("Invalid code.")
    method.last_used_timestep = result.timestep
    await limiter.clear_mfa_failures(key)


async def _fresh_recovery_codes(db: AsyncSession, user_id: int) -> list[str]:
    await db.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user_id))
    codes = totp_service.generate_recovery_codes()
    for code in codes:
        db.add(
            MfaRecoveryCode(
                user_id=user_id,
                code_hash=hash_token(totp_service.normalise_recovery_code(code)),
            )
        )
    return codes


@router.get("/status", response_model=MfaStatus)
async def mfa_status(session: SignedIn) -> MfaStatus:
    async with session_scope() as db:
        method = await _confirmed(db, session.user_id)
        remaining = 0
        if method is not None:
            remaining = len(
                (
                    await db.execute(
                        select(MfaRecoveryCode.id).where(
                            MfaRecoveryCode.user_id == session.user_id,
                            MfaRecoveryCode.used_at.is_(None),
                        )
                    )
                ).all()
            )
    return MfaStatus(
        enabled=method is not None,
        recovery_codes_remaining=remaining,
        can_disable=not _role_requires_it(session),
    )


@router.post("/enrol", response_model=EnrolmentOut)
async def enrol(session: Enrolling) -> EnrolmentOut:
    """Start enrolment. Nothing is switched on until `confirm` succeeds.

    A half-finished enrolment is an UNCONFIRMED method, which login ignores —
    so abandoning this screen can never lock anyone out. Starting again
    replaces it.
    """
    async with session_scope() as db:
        if await _confirmed(db, session.user_id) is not None:
            raise ConflictError(
                "Two-factor authentication is already on. Turn it off first to "
                "move it to a new device."
            )
        await db.execute(
            delete(MfaMethod).where(
                MfaMethod.user_id == session.user_id, MfaMethod.confirmed_at.is_(None)
            )
        )
        secret = totp_service.generate_secret()
        db.add(
            MfaMethod(
                user_id=session.user_id,
                method_type="totp",
                secret_encrypted=encrypt_for_tenant(session.tenant_id, secret),
            )
        )

    uri = totp_service.provisioning_uri(
        secret, f"{session.username}@{session.tenant_slug}", ISSUER
    )
    svg = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage).to_string(
        encoding="unicode"
    )
    return EnrolmentOut(secret=secret, otpauth_uri=uri, qr_svg=str(svg))


@router.post("/confirm", response_model=RecoveryCodesOut)
async def confirm(payload: CodeIn, session: Enrolling, limiter: Limiter) -> RecoveryCodesOut:
    """Prove the app works: one valid code switches the factor on.

    Also satisfies the current session — the user just answered a challenge —
    which is what releases a session that was pending only for enrolment.
    """
    async with session_scope() as db:
        if await _confirmed(db, session.user_id) is not None:
            raise ConflictError("Two-factor authentication is already on.")
        pending = (
            (
                await db.execute(
                    select(MfaMethod)
                    .where(MfaMethod.user_id == session.user_id, MfaMethod.confirmed_at.is_(None))
                    .order_by(MfaMethod.id.desc())
                )
            )
            .scalars()
            .first()
        )
        if pending is None:
            raise ValidationError("Start enrolment first.")
        if "-" in payload.code:
            raise ValidationError("Enter the 6-digit code from your authenticator app.")

        await _prove(db, session, pending, payload.code, limiter)
        pending.confirmed_at = datetime.now(UTC)
        codes = await _fresh_recovery_codes(db, session.user_id)
        await mark_mfa_satisfied(db, session.session_id)

        from suliko.core.audit import record

        await record(
            db, session, action="user.mfa_enabled", entity_type="user", entity_id=session.user_id
        )

    log.info("mfa_enabled", user_id=session.user_id, tenant_id=session.tenant_id)
    return RecoveryCodesOut(recovery_codes=codes)


@router.post("/recovery-codes", response_model=RecoveryCodesOut)
async def regenerate_recovery_codes(
    payload: CodeIn, session: SignedIn, limiter: Limiter
) -> RecoveryCodesOut:
    """Replace every recovery code. The old ones stop working at once."""
    async with session_scope() as db:
        method = await _confirmed(db, session.user_id)
        if method is None:
            raise ValidationError("Two-factor authentication is not on.")
        await _prove(db, session, method, payload.code, limiter)
        codes = await _fresh_recovery_codes(db, session.user_id)

        from suliko.core.audit import record

        await record(
            db,
            session,
            action="user.mfa_recovery_regenerated",
            entity_type="user",
            entity_id=session.user_id,
        )
    return RecoveryCodesOut(recovery_codes=codes)


@router.post("/disable", status_code=204)
async def disable(payload: CodeIn, session: SignedIn, limiter: Limiter) -> None:
    """Switch the factor off, with a current code as proof.

    Refused while the role requires a factor and enrolment is being enforced:
    otherwise "off" is one click away from the protection it exists to give.
    """
    if _role_requires_it(session):
        raise ConflictError(
            "Your role requires two-factor authentication, so it cannot be switched off. "
            "To move it to a new phone, ask an administrator to reset it."
        )
    async with session_scope() as db:
        method = await _confirmed(db, session.user_id)
        if method is None:
            raise ValidationError("Two-factor authentication is not on.")
        await _prove(db, session, method, payload.code, limiter)
        await db.execute(delete(MfaMethod).where(MfaMethod.user_id == session.user_id))
        await db.execute(delete(MfaRecoveryCode).where(MfaRecoveryCode.user_id == session.user_id))

        from suliko.core.audit import record

        await record(
            db, session, action="user.mfa_disabled", entity_type="user", entity_id=session.user_id
        )
    log.info("mfa_disabled", user_id=session.user_id, tenant_id=session.tenant_id)


__all__ = ["router"]
