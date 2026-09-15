"""Dependencies for the translator portal and the suliko.ge admin endpoints.

Portal requests carry no CRM session, so the chain in ``api/deps.py`` does not
apply: no tenant is bound from a session and ``get_db`` is never used. Instead:

    get_portal_identity        verify the signed assertion (or a file ticket)
      -> get_platform_db       a session with NO tenant bound
      -> TenantSessions        enter one bureau's scope at a time, on demand

The platform session reads platform tables only — portal translators, their
links, personal orders. Anything a bureau owns is read through
``TenantSessions``, which binds the tenant *before* opening the connection: the
ordering rule ``api/deps.py`` documents, for the same reason. The RLS setting
is written when the connection is opened.

The tenant ids handed to ``TenantSessions`` come from ``portal_translator_links``
rows, never from the request. A request names a bureau by slug, and the slug
only selects among the caller's own links.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.config import get_settings
from suliko.core.errors import AuthenticationError, PermissionDeniedError
from suliko.db.session import session_scope
from suliko.db.tenancy import TenantContextError, tenant_scope, try_get_current_tenant_id
from suliko.integrations.google_drive import DriveClient, get_drive_client
from suliko.security.portal_tokens import PortalTokenError, verify_token

log = structlog.get_logger()

ASSERTION_HEADER = "X-Suliko-Portal-Assertion"
TICKET_PARAM = "ticket"


@dataclass(frozen=True, slots=True)
class PortalIdentity:
    #: The suliko.ge (.NET backend) user id.
    user_id: str
    #: True only on an assertion, and only when suliko.ge says the user is an
    #: admin. A browser-held ticket never carries it.
    is_admin: bool


def _portal_secret() -> str:
    secret = get_settings().portal_shared_secret.get_secret_value()
    if not secret:
        raise AuthenticationError("The translator portal is not enabled on this server.")
    return secret


def _rejected(request: Request, reason: str) -> AuthenticationError:
    # The reason goes to the log; the caller gets one flat message, so a forger
    # cannot tell a bad signature from an expired token.
    log.warning("portal_credentials_rejected", reason=reason, path=request.url.path)
    return AuthenticationError("Invalid or expired portal credentials.")


async def get_portal_identity(request: Request) -> PortalIdentity:
    """Server-to-server calls: the assertion header, and nothing else."""
    secret = _portal_secret()
    token = request.headers.get(ASSERTION_HEADER)
    if not token:
        raise AuthenticationError("Missing portal credentials.")
    try:
        claims = verify_token(
            secret,
            token,
            expected_type="assertion",
            max_age_seconds=get_settings().portal_assertion_max_age_seconds,
        )
    except PortalTokenError as exc:
        raise _rejected(request, str(exc)) from exc
    return PortalIdentity(user_id=claims.user_id, is_admin=claims.is_admin)


async def get_portal_file_identity(request: Request) -> PortalIdentity:
    """File transfer: an assertion, or a ticket bound to this exact request.

    Only file routes accept tickets. A ticket is held by a browser, so it is
    confined to the one method and path it was issued for — it cannot list
    orders, cannot reach another file, and never carries admin rights.
    """
    if request.headers.get(ASSERTION_HEADER):
        return await get_portal_identity(request)

    secret = _portal_secret()
    ticket = request.query_params.get(TICKET_PARAM)
    if not ticket:
        raise AuthenticationError("Missing portal credentials.")
    try:
        claims = verify_token(
            secret,
            ticket,
            expected_type="ticket",
            max_age_seconds=get_settings().portal_ticket_max_age_seconds,
        )
    except PortalTokenError as exc:
        raise _rejected(request, str(exc)) from exc

    if claims.method != request.method.upper() or claims.path != request.url.path:
        raise _rejected(request, "ticket presented for a different request")
    return PortalIdentity(user_id=claims.user_id, is_admin=False)


async def require_portal_admin(
    identity: Annotated[PortalIdentity, Depends(get_portal_identity)],
) -> PortalIdentity:
    if not identity.is_admin:
        raise PermissionDeniedError("This requires a suliko.ge administrator.")
    return identity


async def get_platform_db() -> AsyncIterator[AsyncSession]:
    """A session for platform tables, with no tenant bound."""
    if try_get_current_tenant_id() is not None:
        # Portal routes never bind a tenant from a session. If one is bound
        # here, something upstream is wrong — fail rather than guess.
        raise TenantContextError("A platform session was requested inside a tenant scope.")
    async with session_scope() as db:
        yield db


TenantSessionFactory = Callable[[int], AbstractAsyncContextManager[AsyncSession]]


@asynccontextmanager
async def open_tenant_session(tenant_id: int) -> AsyncIterator[AsyncSession]:
    """Enter one bureau's scope and open a connection inside it, for one block."""
    with tenant_scope(tenant_id):
        async with session_scope() as db:
            yield db


def get_tenant_sessions() -> TenantSessionFactory:
    """FastAPI dependency; tests substitute a factory bound to their database."""
    return open_tenant_session


PortalCaller = Annotated[PortalIdentity, Depends(get_portal_identity)]
PortalFileCaller = Annotated[PortalIdentity, Depends(get_portal_file_identity)]
PortalAdmin = Annotated[PortalIdentity, Depends(require_portal_admin)]
PlatformDb = Annotated[AsyncSession, Depends(get_platform_db)]
TenantSessions = Annotated[TenantSessionFactory, Depends(get_tenant_sessions)]
Drive = Annotated[DriveClient, Depends(get_drive_client)]
