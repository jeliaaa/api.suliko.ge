"""Request dependencies.

The ordering in this module is the security model. Read it once carefully:

    get_bearer_token          extract the opaque token
      -> get_current_session  resolve it, BIND THE TENANT CONTEXT
        -> get_db             open a session (the GUC reads the bound tenant)
          -> require(perm)    authorise

The tenant must be bound *before* the database session is opened, because
``session_scope`` writes ``suliko.tenant_id`` — the setting the RLS policies
read — at connection time. Open the DB session first and RLS sees no tenant.

``get_current_session`` uses its own short-lived DB session for exactly this
reason: it cannot depend on ``get_db`` without creating that ordering problem.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.config import get_settings
from suliko.core.errors import (
    AuthenticationError,
    MfaRequiredError,
    PermissionDeniedError,
    StepUpRequiredError,
)
from suliko.db.session import get_sessionmaker, session_scope
from suliko.db.tenancy import reset_current_tenant_id, set_current_tenant_id
from suliko.security.permissions import Permission, requires_step_up
from suliko.security.sessions import AuthenticatedSession, resolve_session


def get_client_ip(request: Request) -> str | None:
    """The caller's IP.

    Only the BFF talks to this API, so ``X-Forwarded-For`` is set by our own
    proxy and can be trusted — but only the FIRST hop, and only because the
    deployment terminates TLS in front of us. If this API is ever exposed
    directly, this must stop trusting the header.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def get_bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


async def get_current_session(
    request: Request,
    token: Annotated[str | None, Depends(get_bearer_token)],
) -> AsyncIterator[AuthenticatedSession]:
    """Resolve the session and bind the tenant for the rest of the request.

    The ContextVar is reset in a ``finally`` so a worker task cannot inherit
    the previous request's tenant.
    """
    if token is None:
        raise AuthenticationError("Missing bearer token.")

    async with get_sessionmaker()() as lookup_db:
        session = await resolve_session(lookup_db, token)
        await lookup_db.commit()

    if session is None:
        raise AuthenticationError("Invalid or expired session.")

    tenant_token = set_current_tenant_id(session.tenant_id)
    request.state.session = session
    try:
        yield session
    finally:
        reset_current_tenant_id(tenant_token)


async def get_db(
    _session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> AsyncIterator[AsyncSession]:
    """A tenant-bound database session.

    Depends on ``get_current_session`` purely for ordering — the tenant must
    be in context before the connection sets its GUC.
    """
    async with session_scope() as db:
        yield db


async def get_authenticated_session(
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> AuthenticatedSession:
    """A session that has cleared its second factor.

    A half-authenticated session (password accepted, 2FA pending) can reach
    only the challenge endpoint, which depends on ``get_current_session``
    directly rather than on this.

    The test is ``mfa_satisfied_at is None`` alone, with no role check. Login
    stamps that field only when the user has NO enrolled factor AND their role
    does not require one — so a null value already means "this user owes a
    second factor", for either reason.

    Checking the role here as well would be actively wrong: a staff user who
    voluntarily enrolled TOTP would be let through on their password alone,
    silently ignoring the factor they chose to add.
    """
    if not get_settings().mfa_enforced:
        # Checked here as well as at login so that sessions issued BEFORE the
        # switch was flipped are not left permanently stuck on a challenge
        # that no longer exists.
        return session

    if session.mfa_satisfied_at is None:
        raise MfaRequiredError("Two-factor authentication is required to continue.")
    return session


CurrentSession = Annotated[AuthenticatedSession, Depends(get_authenticated_session)]
Db = Annotated[AsyncSession, Depends(get_db)]


def require(*permissions: Permission) -> Callable[..., Awaitable[AuthenticatedSession]]:
    """Require every listed permission, plus step-up where applicable.

    Usage::

        @router.post(
            "/payments",
            dependencies=[Depends(require(Permission.FINANCE_RECORD_PAYMENT))],
        )

    or, when the handler needs the session::

        session: Annotated[AuthenticatedSession, Depends(require(Permission.ORDERS_WRITE))]
    """

    async def dependency(
        session: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    ) -> AuthenticatedSession:
        settings = get_settings()

        for permission in permissions:
            if not session.has(permission):
                # Within the caller's own tenant, so 403 is right — there is
                # no cross-tenant existence to leak here.
                raise PermissionDeniedError(
                    f"This action requires the {permission.value} permission."
                )

            if requires_step_up(permission):
                age = session.mfa_age_seconds(datetime.now(UTC))
                max_age = settings.step_up_max_age_minutes * 60
                if age is None or age > max_age:
                    raise StepUpRequiredError(
                        "Re-enter your authentication code to continue.",
                        permission=permission.value,
                    )

        return session

    return dependency
