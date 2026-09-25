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

import hmac
import ipaddress
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.config import get_settings
from suliko.core.errors import (
    AuthenticationError,
    MfaRequiredError,
    PasswordChangeRequiredError,
    PermissionDeniedError,
    StepUpRequiredError,
)
from suliko.core.gateway import HEADER_NAME as GATEWAY_HEADER
from suliko.db.session import get_sessionmaker, session_scope
from suliko.db.tenancy import reset_current_tenant_id, set_current_tenant_id
from suliko.domain.plans import Feature, allows_feature
from suliko.security.permissions import Permission, requires_step_up
from suliko.security.sessions import AuthenticatedSession, resolve_session

#: Set by the BFF to the BROWSER's address and user agent. Without them every
#: request looks like it came from the BFF's own server — so the per-IP login
#: and sign-up limits were shared by every user of every bureau at once: 20
#: failed logins anywhere locked everybody out, and the whole platform could
#: take 3 sign-ups an hour.
CLIENT_IP_HEADER = "x-suliko-client-ip"
CLIENT_UA_HEADER = "x-suliko-client-ua"


def _parse_ip(value: str | None) -> str | None:
    """A normalised IP, or None.

    The value lands in PostgreSQL `INET` columns, where anything that is not
    an address is a 500 on login. IIS ARR appends the port (``1.2.3.4:5678``)
    and bracketed IPv6 carries one too (``[::1]:443``).
    """
    if not value:
        return None
    candidate = value.strip()
    if candidate.startswith("["):
        candidate = candidate[1:].split("]", 1)[0]
    elif candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _from_our_bff(request: Request) -> bool:
    """Whether the BFF-only headers can be believed on this request.

    With a gateway secret configured, only a request that presented it; with
    none, the API is loopback-only by configuration and every caller is ours.
    """
    expected = get_settings().bff_shared_secret.get_secret_value()
    if not expected:
        return True
    return hmac.compare_digest(request.headers.get(GATEWAY_HEADER, ""), expected)


def get_client_ip(request: Request) -> str | None:
    """The caller's IP.

    The BFF's own header first — it is the only place the browser's address
    survives the hop through the BFF — and only when the request proved it
    came from the BFF. Then the first ``X-Forwarded-For`` hop, set by the
    reverse proxy in front of us (the path portal-ticket requests from
    browsers take). Anything unparseable is dropped rather than stored.
    """
    if _from_our_bff(request):
        from_bff = _parse_ip(request.headers.get(CLIENT_IP_HEADER))
        if from_bff:
            return from_bff
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = _parse_ip(forwarded.split(",")[0])
        if first:
            return first
    return _parse_ip(request.client.host) if request.client else None


def get_client_user_agent(request: Request) -> str | None:
    """The browser's user agent, for the session and login-attempt records."""
    if _from_our_bff(request):
        forwarded = request.headers.get(CLIENT_UA_HEADER)
        if forwarded:
            return forwarded[:255]
    return request.headers.get("user-agent")


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


async def _require_mfa(session: AuthenticatedSession) -> AuthenticatedSession:
    """The second-factor half of the gate.

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


async def get_session_for_password_change(
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> AuthenticatedSession:
    """Cleared its second factor, but may still owe a password change.

    Exists so `POST /auth/password/change` is reachable by the one person the
    gate below is aimed at. Everything else depends on
    `get_authenticated_session`, which refuses them — otherwise an invited
    employee could work indefinitely on a password their manager chose and
    still knows.
    """
    return await _require_mfa(session)


async def get_authenticated_session(
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> AuthenticatedSession:
    """A session that has cleared its second factor AND owns its password.

    The gate every business endpoint sits behind. Two refusals, and they are
    deliberately separate dependencies so that each one's own escape hatch
    stays reachable: the 2FA challenge depends on ``get_current_session``, and
    the change-password endpoint on ``get_session_for_password_change``.
    """
    session = await _require_mfa(session)

    if session.must_change_password:
        # An invite's one-time password, or one an administrator set. Both are
        # known to somebody else, so the account is not yet the user's alone.
        raise PasswordChangeRequiredError("Set your own password before continuing.")
    return session


CurrentSession = Annotated[AuthenticatedSession, Depends(get_authenticated_session)]

#: `scope="function"` is load-bearing. `get_db` commits when it exits, and a
#: yield dependency's default ("request") scope exits only AFTER the response
#: has been sent (FastAPI >= 0.118). With that default, a write the database
#: refuses at COMMIT — a RESTRICT foreign key on delete, a unique index — has
#: already been answered 2xx, then silently rolls back; and a create followed
#: by an immediate read can race the commit and 404. Function scope commits
#: before the response exists, so the answer reflects what was stored.
Db = Annotated[AsyncSession, Depends(get_db, scope="function")]


def require_feature(feature: Feature) -> Callable[..., Awaitable[AuthenticatedSession]]:
    """Require the caller's PLAN to include a feature.

    The companion to ``require`` for screens that no permission guards.
    Dashboard, Calculator and Notifications are open to every role, so the
    plan's permission mask cannot reach them and they need naming explicitly.

    403 rather than 404: the caller is inside their own tenant and the feature
    demonstrably exists — they are on the wrong plan, and telling them so is
    the difference between an upgrade and a support ticket.
    """

    async def dependency(
        session: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    ) -> AuthenticatedSession:
        if not allows_feature(session.plan, feature):
            raise PermissionDeniedError(
                f"{feature.value.replace('_', ' ').capitalize()} is not included "
                f"in the {session.plan.value} plan."
            )
        return session

    return dependency


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

            if requires_step_up(permission) and session.can_step_up:
                age = session.mfa_age_seconds(datetime.now(UTC))
                max_age = settings.step_up_max_age_minutes * 60
                if age is None or age > max_age:
                    raise StepUpRequiredError(
                        "Re-enter your authentication code to continue.",
                        permission=permission.value,
                    )
            elif requires_step_up(permission):
                # No factor to re-present, so there is nothing to step up
                # with. Demanding one anyway does not raise the bar: it
                # refuses Settings, Users, the plan choice and outbound
                # transfers permanently, five minutes after every login, with
                # no action the user can take. Logged rather than silent —
                # this is a control that is not running.
                structlog.get_logger().info(
                    "step_up_skipped_no_factor",
                    user_id=session.user_id,
                    tenant_id=session.tenant_id,
                    permission=permission.value,
                )

        return session

    return dependency
