"""Authentication: sign-in, the organisation chooser, 2FA, logout.

A person has ONE account (email and password, `models.user.Account`) and a
membership row in each organisation they belong to. Sign-in follows that:

    POST /auth/login          email + password -> a ticket and the choices
    POST /auth/sso/exchange   a one-time code from suliko.ge -> the same ticket and choices
    POST /auth/login/select   ticket + a choice -> a session for that membership
    POST /auth/mfa/verify     TOTP code -> that same session, MFA satisfied
    POST /auth/switch         signed in -> a session in another of their organisations

A session with MFA pending can reach nothing except the challenge. That is
enforced by ``get_authenticated_session``, which every other route depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from pydantic import BaseModel, EmailStr, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import (
    Db,
    get_client_ip,
    get_client_user_agent,
    get_current_session,
    get_session_for_password_change,
)
from suliko.config import get_settings
from suliko.core import mail
from suliko.core.crypto import decrypt_for_tenant
from suliko.core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)
from suliko.core.ratelimit import RateLimiter, get_rate_limiter
from suliko.db.session import bind_tenant_guc, get_sessionmaker, session_scope
from suliko.db.tenancy import bypass_tenant_scope, tenant_scope
from suliko.domain.accounts import (
    UNUSABLE_PASSWORD_HASH,
    Membership,
    create_bureau,
    create_personal_workspace,
    find_account,
    find_account_by_login,
    find_account_by_suliko_id,
    login_name,
    memberships,
    normalise_email,
    personal,
    revoke_account_sessions,
    upsert_from_suliko,
    username_for,
)
from suliko.domain.plans import TenantPlan, effective_permissions, effective_plan
from suliko.domain.portal import registration_url
from suliko.integrations.suliko_backend import (
    PasswordOutcome,
    SulikoBackend,
    SulikoUnavailableError,
    SulikoUser,
    get_suliko_backend,
)
from suliko.models.tenant import Tenant
from suliko.models.user import (
    Account,
    LoginAttempt,
    MfaMethod,
    MfaRecoveryCode,
    Role,
    User,
    UserPermissionOverride,
)
from suliko.security import login_tickets, reset_tokens
from suliko.security import totp as totp_service
from suliko.security.passwords import (
    hash_password_async,
    hash_token,
    validate_password_strength,
    verify_and_maybe_rehash_async,
    waste_time_verifying_async,
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


class PasswordManagedExternallyError(ConflictError):
    """The password of a suliko.ge account is changed on suliko.ge, not here."""

    error_code = "password_managed_by_suliko"


class LoginRequest(BaseModel):
    #: What the person signs in with: an email address or a phone number —
    #: whatever they use on suliko.ge.
    identifier: str | None = Field(default=None, min_length=1, max_length=255)
    #: The old name for `identifier`. Still accepted, so this API can be
    #: deployed before the frontend that sends the new one.
    email: str | None = Field(default=None, min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _has_a_login(self) -> LoginRequest:
        if not self.login:
            raise ValueError("identifier is required")
        return self

    @property
    def login(self) -> str:
        return (self.identifier or self.email or "").strip()


class SsoExchangeRequest(BaseModel):
    """What the Office frontend brings back from suliko.ge (see `sso_exchange`)."""

    code: str = Field(min_length=1, max_length=200)
    #: The PKCE verifier the frontend kept in an HttpOnly cookie while the
    #: browser was away. RFC 7636 sets the length.
    code_verifier: str = Field(min_length=43, max_length=128)
    #: The callback address the code was issued for — suliko.ge checks it.
    redirect_uri: str = Field(min_length=1, max_length=500)


class OrgChoice(BaseModel):
    """One place the person can sign in to."""

    tenant_slug: str
    tenant_name: str
    role: str
    is_personal: bool = False


class LoginOptions(BaseModel):
    """Step one's answer: the password was right — now, which organisation?

    `ticket` proves it for ten minutes (see `security/login_tickets.py`).
    `personal` is the person's own freelancer workspace; `personal_exists`
    false means picking it creates one.
    """

    ticket: str
    #: Their address, if they have one — a phone-only person has none.
    email: str | None
    #: What they sign in with: the address, else the phone. Always set.
    login: str
    full_name: str
    organizations: list[OrgChoice]
    personal: OrgChoice | None
    personal_exists: bool


class TicketRequest(BaseModel):
    ticket: str = Field(min_length=1, max_length=300)


class SelectRequest(BaseModel):
    ticket: str = Field(min_length=1, max_length=300)
    #: An organisation's slug, or omitted with `personal` set.
    tenant_slug: str | None = Field(default=None, max_length=63)
    personal: bool = False


class CreateBureauRequest(BaseModel):
    ticket: str = Field(min_length=1, max_length=300)
    organisation_name: str = Field(min_length=2, max_length=255)


class NewBureauRequest(BaseModel):
    organisation_name: str = Field(min_length=2, max_length=255)


class SwitchRequest(BaseModel):
    tenant_slug: str | None = Field(default=None, max_length=63)
    personal: bool = False


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
    tenant_slug: str
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


def _choice(membership: Membership) -> OrgChoice:
    return OrgChoice(
        tenant_slug=membership.tenant.slug,
        tenant_name=membership.tenant.display_name,
        role=membership.user.role.value,
        is_personal=membership.tenant.is_personal,
    )


async def _choices(
    db: AsyncSession, account_id: int
) -> tuple[list[OrgChoice], OrgChoice | None]:
    """Organisations first, the personal workspace apart — the chooser shows
    it separately, and offers to create it when it does not exist."""
    options = await memberships(db, account_id)
    own = personal(options)
    organizations = [_choice(m) for m in options if m is not own]
    return organizations, _choice(own) if own else None


async def _login_options(db: AsyncSession, account: Account, ticket: str) -> LoginOptions:
    organizations, own = await _choices(db, account.id)
    return LoginOptions(
        ticket=ticket,
        email=account.email,
        login=login_name(account),
        full_name=account.full_name,
        organizations=organizations,
        personal=own,
        personal_exists=own is not None,
    )


async def _account_for_suliko_user(
    db: AsyncSession,
    backend: SulikoBackend,
    suliko_user_id: str,
    person: SulikoUser | None = None,
) -> Account:
    """The account of a person suliko.ge has just vouched for, made on first sight.

    Found by suliko.ge id; failing that, asked about (the directory says who
    they are) unless suliko.ge already said so in `person`, and put through
    the one rule in `upsert_from_suliko`. Raises SulikoUnavailableError if
    suliko.ge cannot say who they are — never a guess.
    """
    account = await find_account_by_suliko_id(db, suliko_user_id)
    if account is not None:
        return account

    if person is None:
        person = await backend.get_user(suliko_user_id)
    if person is None:
        # It accepted the password a moment ago and now does not know them:
        # not something to turn into a login or a refusal.
        log.error("suliko_user_vanished")
        raise SulikoUnavailableError("Sign-in is unavailable right now. Try again in a minute.")
    try:
        return (await upsert_from_suliko(db, person)).account
    except IntegrityError:
        # The same person's first two sign-ins raced; the other one won.
        await db.rollback()
        account = await find_account_by_suliko_id(db, suliko_user_id)
        if account is None:
            raise
        return account


async def _authenticate(
    db: AsyncSession, backend: SulikoBackend, login: str, password: str
) -> tuple[Account | None, bool]:
    """Who a login and password belong to: `(account, False)`, `(None, False)`
    for a wrong one, or `(None, True)` when suliko.ge could not be asked.

    suliko.ge's password wins wherever there is one: a right answer there is a
    sign-in whatever Office holds. Failing that, only an account with no link
    to suliko.ge (a platform operator, anyone invited before this existed) may
    still use a password of its own.
    """
    check = await backend.check_password(login, password) if backend.enabled else None
    if check is not None and check.accepted and check.user_id:
        return await _account_for_suliko_user(db, backend, check.user_id), False

    account = await find_account_by_login(db, login)
    if account is not None and account.suliko_user_id is None:
        ok, new_hash = await verify_and_maybe_rehash_async(password, account.password_hash)
        if ok:
            # Transparent bcrypt -> Argon2id upgrade, on the person's own login.
            if new_hash is not None:
                account.password_hash = new_hash
                log.info("password_rehashed", account_id=account.id)
            return account, False
    else:
        # Nothing to verify locally; spend the time anyway so a login that
        # does not exist costs what one that does.
        await waste_time_verifying_async()

    if check is not None and check.outcome is PasswordOutcome.UNAVAILABLE:
        # The password may well have been right, only unprovable just now.
        return None, True
    return None, False


@router.post("/login", response_model=LoginOptions)
async def login(
    payload: LoginRequest,
    request: Request,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    backend: Annotated[SulikoBackend, Depends(get_suliko_backend)],
) -> LoginOptions:
    """Step one: email or phone, and password — for the person, not an organisation.

    The password is checked by suliko.ge for everyone registered there (see
    `integrations/suliko_backend.py`), and by Office itself only for accounts
    with no suliko.ge link.

    Failure is uniform: the same message, the same status, and approximately
    the same latency whether the login or the password was wrong. Anything
    else enumerates accounts. suliko.ge being unreachable is the one honest
    exception — a 503, not counted against the person's attempts.

    Success is not a session yet. It is a ticket and the list of places the
    person can enter; `POST /auth/login/select` turns one into a session.
    """
    ip = get_client_ip(request)
    user_agent = get_client_user_agent(request)
    login_text = payload.login
    login_key = login_text.lower()

    # Per-account and per-IP, so one attacker cannot lock a real user out
    # platform-wide by hammering their address from everywhere.
    account_key = f"login:acct:{login_key}"
    ip_key = f"login:ip:{ip or 'unknown'}"
    if retry := await limiter.check_login(account_key, ip_key):
        raise RateLimitedError("Too many attempts. Try again later.", retry_after=retry)

    async with get_sessionmaker()() as db:
        account, unavailable = await _authenticate(db, backend, login_text, payload.password)

        if unavailable:
            await _record_attempt(db, login_key, ip, user_agent, False, "upstream_unavailable")
            await db.commit()
            raise SulikoUnavailableError(
                "Sign-in is temporarily unavailable. Try again in a minute."
            )

        if account is None:
            await limiter.record_login_failure(account_key, ip_key)
            await _record_attempt(db, login_key, ip, user_agent, False, "bad_credentials")
            await db.commit()
            raise AuthenticationError("Invalid email, phone or password.")

        await _record_attempt(db, login_key, ip, user_agent, True)
        await limiter.clear_login_failures(account_key)
        options = await _login_options(
            db, account, login_tickets.issue(account.id, account.password_hash)
        )
        await db.commit()
    return options


@router.post("/sso/exchange", response_model=LoginOptions)
async def sso_exchange(
    payload: SsoExchangeRequest,
    request: Request,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    backend: Annotated[SulikoBackend, Depends(get_suliko_backend)],
) -> LoginOptions:
    """Step one, the suliko.ge way: a one-time code instead of a password.

    The person signed in on suliko.ge — with a password, or with Google, which
    gives them no password Office could check — and suliko.ge sent them back
    here with a code. suliko.ge alone can say whose it is (see
    `SulikoBackend.redeem_sso_code`): it is spent on the first try, lives a
    minute, and is redeemable only with the verifier this frontend kept, so a
    code lifted from a URL is worth nothing. Office never sees a password.

    What follows is exactly `POST /auth/login`'s: a ticket and the places the
    person can enter.
    """
    ip = get_client_ip(request)
    user_agent = get_client_user_agent(request)

    # A code is unguessable, so this only bounds someone replaying junk.
    code_key = f"login:sso:{ip or 'unknown'}"
    ip_key = f"login:ip:{ip or 'unknown'}"
    if retry := await limiter.check_login(code_key, ip_key):
        raise RateLimitedError("Too many attempts. Try again later.", retry_after=retry)

    async with get_sessionmaker()() as db:
        try:
            person = await backend.redeem_sso_code(
                payload.code, payload.code_verifier, payload.redirect_uri
            )
            account = (
                None
                if person is None
                else await _account_for_suliko_user(db, backend, person.id, person)
            )
        except SulikoUnavailableError:
            await _record_attempt(db, "suliko.ge", ip, user_agent, False, "upstream_unavailable")
            await db.commit()
            raise

        if account is None:
            await limiter.record_login_failure(code_key, ip_key)
            await _record_attempt(db, "suliko.ge", ip, user_agent, False, "bad_sso_code")
            await db.commit()
            raise AuthenticationError("Your sign-in has expired. Sign in again.")

        await _record_attempt(db, login_name(account), ip, user_agent, True)
        options = await _login_options(
            db, account, login_tickets.issue(account.id, account.password_hash)
        )
        await db.commit()
    return options


async def _account_from_ticket(db: AsyncSession, ticket: str) -> Account:
    """The account a ticket vouches for, or a 401 that sends them back to sign in."""
    try:
        account_id = login_tickets.read(ticket)
    except login_tickets.TicketError:
        raise AuthenticationError("Your sign-in has expired. Sign in again.") from None
    account = await db.get(Account, account_id)
    if account is None or not login_tickets.matches(ticket, account.password_hash):
        raise AuthenticationError("Your sign-in has expired. Sign in again.")
    return account


@router.post("/login/options", response_model=LoginOptions)
async def login_options(payload: TicketRequest) -> LoginOptions:
    """The chooser's list again, for a page reload between the two steps."""
    async with get_sessionmaker()() as db:
        account = await _account_from_ticket(db, payload.ticket)
        return await _login_options(db, account, payload.ticket)


def _personal_enabled_email(account: Account, link: str) -> tuple[str, str]:
    """Subject and plain-text body. No HTML — see core/mail.py."""
    body = (
        f"Hello {account.full_name or login_name(account)},\n\n"
        "Your personal Suliko account is now enabled, on the Freelancer plan. "
        "It is yours alone: your own clients, orders and prices, separate from "
        "any bureau you work with.\n\n"
        f"Open it any time from the organisation switcher, or sign in here:\n\n{link}\n\n"
        "If you did not do this, sign in and change your password.\n"
    )
    return "Your personal Suliko account is enabled", body


async def _resolve_choice(
    db: AsyncSession,
    account: Account,
    *,
    tenant_slug: str | None,
    want_personal: bool,
    background: BackgroundTasks,
) -> Membership:
    """The membership a pick refers to — creating the personal workspace on
    its first pick. Only ever one of the account's own: a slug it does not
    belong to is refused exactly like one that does not exist."""
    options = await memberships(db, account.id)
    if want_personal:
        own = personal(options)
        if own is not None:
            return own
        own = await create_personal_workspace(db, account)

        from suliko.core.audit import record

        await record(
            db,
            None,
            action="tenant.personal_created",
            entity_type="tenant",
            entity_id=own.tenant.id,
            tenant_id=own.tenant.id,
            after={"slug": own.tenant.slug, "account_id": account.id},
        )
        settings = get_settings()
        link = f"{settings.app_url.rstrip('/')}/{own.tenant.locale}/login"
        subject, body = _personal_enabled_email(account, link)
        # After the response, which is after the commit: the email must not
        # announce a workspace a rolled-back transaction never created. Someone
        # who signs in with a phone number has no address to tell.
        if account.email:
            background.add_task(mail.send, account.email, subject, body)
        log.info("personal_workspace_created", account_id=account.id, tenant_id=own.tenant.id)
        return own

    slug = (tenant_slug or "").strip().lower()
    for membership in options:
        if membership.tenant.slug == slug:
            return membership
    raise NotFoundError("You do not have access to that organisation.")


async def _start_session(
    db: AsyncSession,
    membership: Membership,
    account: Account,
    *,
    ip: str | None,
    user_agent: str | None,
) -> LoginResponse:
    """A session for one membership — the second factor and permissions exactly
    as that organisation's row has them."""
    user, tenant = membership.user, membership.tenant

    # This session may have been opened before we knew which tenant we were
    # acting for, so it carries no RLS GUC. Everything below writes
    # tenant-scoped rows — set it now, or the `tenant_isolation` policy rejects
    # them the moment the app stops connecting as a PostgreSQL superuser.
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
        # Note this covers VOLUNTARY enrolment: a staff user who added TOTP is
        # challenged even though their role does not demand it.
        challenge_owed = enforced and has_mfa

        # The role demands a factor and none is enrolled. Failing closed is the
        # stronger policy, gated behind MFA_REQUIRE_ENROLMENT because it is
        # only honest once a user can enrol for themselves.
        enrolment_required = require_enrolment and must_have_mfa and not has_mfa

        if must_have_mfa and not has_mfa and not require_enrolment:
            # Logged per login: the list of privileged accounts running without
            # a second factor, to work through when enrolment ships.
            log.warning(
                "privileged_login_without_mfa",
                user_id=user.id,
                tenant_id=user.tenant_id,
                tenant_slug=tenant.slug,
                role=user.role.value,
            )

        issued = await create_session(
            db,
            user,
            ip=ip,
            user_agent=user_agent,
            mfa_satisfied=not (challenge_owed or enrolment_required),
        )

        now = datetime.now(UTC)
        user.last_login_at = now
        overrides = {
            row.permission: row.granted
            for row in (
                await db.execute(
                    select(UserPermissionOverride).where(UserPermissionOverride.user_id == user.id)
                )
            ).scalars()
        }
        # Written HERE, inside this membership's scope. On `/auth/switch` the
        # request is bound to the organisation being LEFT, and the commit runs
        # after this block — a `last_login_at` still pending then is a write to
        # another tenant's row, and the tenant guard (rightly) refuses it.
        await db.flush()
    account.last_login_at = now

    return LoginResponse(
        session_token=issued.token,
        # "Go to the challenge screen now" — NOT "a factor exists". With MFA
        # switched off this is false even for a user who has TOTP enrolled.
        mfa_required=challenge_owed,
        mfa_enrolment_required=enrolment_required,
        must_change_password=account.must_change_password,
        user_id=user.id,
        tenant_id=user.tenant_id,
        tenant_slug=tenant.slug,
        role=user.role.value,
        # Masked by the plan and the user's own overrides, exactly as
        # resolve_session computes it — otherwise the first page after login
        # paints a different sidebar from every page after it.
        permissions=sorted(
            p.value
            for p in effective_permissions(user.role, effective_plan(tenant.plan), overrides)
        ),
    )


@router.post("/login/select", response_model=LoginResponse)
async def login_select(
    payload: SelectRequest, request: Request, background: BackgroundTasks
) -> LoginResponse:
    """Step two: turn the ticket and a pick into a session."""
    async with get_sessionmaker()() as db:
        account = await _account_from_ticket(db, payload.ticket)
        membership = await _resolve_choice(
            db,
            account,
            tenant_slug=payload.tenant_slug,
            want_personal=payload.personal,
            background=background,
        )
        response = await _start_session(
            db,
            membership,
            account,
            ip=get_client_ip(request),
            user_agent=get_client_user_agent(request),
        )
        await db.commit()
    return response


async def _found_bureau(db: AsyncSession, account: Account, name: str) -> Membership:
    """`create_bureau` plus its audit entry — shared by the chooser and the
    switcher, so both leave the same trail."""
    created = await create_bureau(db, account, name)

    from suliko.core.audit import record

    await record(
        db,
        None,
        action="tenant.bureau_created",
        entity_type="tenant",
        entity_id=created.tenant.id,
        tenant_id=created.tenant.id,
        after={"slug": created.tenant.slug, "account_id": account.id},
    )
    log.info("bureau_created", account_id=account.id, tenant_id=created.tenant.id)
    return created


@router.post("/login/create-bureau", response_model=LoginResponse)
async def login_create_bureau(payload: CreateBureauRequest, request: Request) -> LoginResponse:
    """Step two, the other way: found a new bureau and enter it as its owner.

    Open to anyone holding a sign-in ticket — in particular someone who
    belongs to no organisation yet, for whom this is the way in. Sign-up
    being closed does not close this: it creates no account.
    """
    async with get_sessionmaker()() as db:
        account = await _account_from_ticket(db, payload.ticket)
        membership = await _found_bureau(db, account, payload.organisation_name)
        response = await _start_session(
            db,
            membership,
            account,
            ip=get_client_ip(request),
            user_agent=get_client_user_agent(request),
        )
        await db.commit()
    return response


@router.post("/organisations", response_model=LoginResponse)
async def create_organisation(
    payload: NewBureauRequest,
    request: Request,
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> LoginResponse:
    """Found a new bureau from inside the app and switch to it, like
    `POST /auth/switch` does for an existing one."""
    if session.account_id is None:
        raise ValidationError("This sign-in is not linked to an account.")
    async with get_sessionmaker()() as db:
        account = await db.get(Account, session.account_id)
        if account is None:
            raise AuthenticationError("Your account is no longer available.")
        membership = await _found_bureau(db, account, payload.organisation_name)
        response = await _start_session(
            db,
            membership,
            account,
            ip=get_client_ip(request),
            user_agent=get_client_user_agent(request),
        )
        await revoke_session(db, session.session_id)
        await db.commit()
    return response


@router.post("/switch", response_model=LoginResponse)
async def switch_organisation(
    payload: SwitchRequest,
    request: Request,
    background: BackgroundTasks,
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> LoginResponse:
    """Move to another of the signed-in person's organisations, or their
    personal account, without typing the password again.

    The new session belongs to the target organisation's own row; the old one
    is revoked, so one browser holds one live session.
    """
    if session.account_id is None:
        raise ValidationError("This sign-in is not linked to an account.")
    async with get_sessionmaker()() as db:
        account = await db.get(Account, session.account_id)
        if account is None:
            raise AuthenticationError("Your account is no longer available.")
        membership = await _resolve_choice(
            db,
            account,
            tenant_slug=payload.tenant_slug,
            want_personal=payload.personal,
            background=background,
        )
        response = await _start_session(
            db,
            membership,
            account,
            ip=get_client_ip(request),
            user_agent=get_client_user_agent(request),
        )
        await revoke_session(db, session.session_id)
        await db.commit()
    return response


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


class EmailTakenError(ConflictError):
    error_code = "email_taken"


def _existing_account(plan: str | None, display_name: str) -> dict[str, str]:
    """How an account already holding a sign-up's address is described to it.

    A bureau is named. A freelancer is not: their workspace is usually named
    after the person, and "freelancer" already tells the visitor to sign in
    instead. An unchosen plan counts as freelancer, as it is enforced.
    """
    if effective_plan(plan) is TenantPlan.BUREAU:
        return {"kind": "bureau", "name": display_name}
    return {"kind": "freelancer"}


def _verification_email(user: User, tenant: Tenant, link: str, ttl_hours: int) -> tuple[str, str]:
    """Subject and plain-text body. No HTML — see core/mail.py."""
    days = ttl_hours // 24
    validity = f"{days} day{'s' if days != 1 else ''}" if days else f"{ttl_hours} hours"
    body = (
        f"Hello {user.full_name or user.username},\n\n"
        f"Welcome to Suliko. Confirm this address to finish setting up "
        f"{tenant.display_name}:\n\n{link}\n\n"
        f"The link works once and expires in {validity}. Nothing about your "
        f"account is on hold while you do this. It is only so we know this "
        f"address is really yours.\n\n"
        f"If you did not sign up for Suliko, you can ignore this email.\n"
    )
    return "Confirm your email for Suliko", body


@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: SignupRequest,
    request: Request,
    background: BackgroundTasks,
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

    # Closed for now (2026-09-30): people join by invitation. The page is
    # hidden too; this makes the endpoint agree rather than rely on that.
    if not settings.signup_enabled:
        raise NotFoundError("Sign-up is closed.")

    ip_key = f"signup:ip:{ip or 'unknown'}"
    if retry := await limiter.check_signup(ip_key):
        raise RateLimitedError("Too many sign-ups from this address.", retry_after=retry)

    problems = validate_password_strength(payload.password)
    if problems:
        raise ValidationError(" ".join(problems))

    email = normalise_email(str(payload.email))
    username = username_for(email)

    await limiter.record_signup(ip_key)

    async with get_sessionmaker()() as db:
        # Across every organisation, not just the new (empty) one. Checked after
        # `record_signup` so each probe of an address spends a sign-up attempt.
        with bypass_tenant_scope():
            holders = (
                await db.execute(
                    select(Tenant.plan, Tenant.display_name)
                    .join(User, User.tenant_id == Tenant.id)
                    .where(func.lower(User.email) == email)
                    .order_by(Tenant.id)
                )
            ).all()
        if holders or await find_account(db, email) is not None:
            accounts: list[dict[str, str]] = []
            for plan, name in holders:
                holder = _existing_account(plan, name)
                if holder not in accounts:
                    accounts.append(holder)
            labels = ", ".join(a.get("name", "Freelancer") for a in accounts)
            raise EmailTakenError(
                f"An account with this email already exists: {labels}.",
                accounts=accounts,
            )

        account = Account(
            email=email,
            password_hash=await hash_password_async(payload.password),
            full_name=payload.full_name.strip(),
        )
        db.add(account)
        await db.flush()

        # The same routine the chooser's "Create a bureau" uses — starter
        # catalogues, this account as OWNER — except that the plan is left
        # NULL ("has not chosen yet"): onboarding sets it.
        created = await create_bureau(db, account, payload.organisation_name, plan=None)
        tenant, user = created.tenant, created.user
        tenant_id, slug = int(tenant.id), tenant.slug

        with tenant_scope(tenant_id):
            # Signed straight in. Sending someone who just chose a password
            # back to a login form to retype it is friction with no security
            # benefit — they proved possession of the password by setting it.
            issued = await create_session(
                db,
                user,
                ip=ip,
                user_agent=get_client_user_agent(request),
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
            user.last_login_at = account.last_login_at = datetime.now(UTC)

            # Minted now so it commits atomically with the user it belongs to;
            # sent below, after the commit — see the note on the same pattern
            # in `forgot_password`.
            verification_token = await reset_tokens.issue(
                db,
                user,
                ttl_seconds=settings.email_verification_ttl_hours * 3600,
                purpose=reset_tokens.EMAIL_VERIFICATION,
            )

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

    # After the commit, same reasoning as `forgot_password`: a link that
    # reaches an inbox before its row is durable is a link that does not
    # work. Not gating anything on this succeeding — see `_verification_email`
    # — so unlike that endpoint there is no timing side-channel to protect
    # against and this can simply run as a background task.
    link = (
        f"{settings.app_url.rstrip('/')}/{tenant.locale}/verify-email"
        f"?token={quote(verification_token, safe='')}"
    )
    subject, body = _verification_email(user, tenant, link, settings.email_verification_ttl_hours)
    background.add_task(mail.send, email, subject, body)

    return SignupResponse(
        session_token=issued.token,
        tenant_slug=slug,
        username=username,
    )


# ── Email verification ──────────────────────────────────────────────────────
#
#   POST /auth/verify-email          spend the signup link       (anonymous)
#   POST /auth/verify-email/resend   mail a fresh one             (signed in)
#
# Nothing in the API refuses anything for want of this today — see the
# docstring on `User.email_verified_at`. This pair exists so the frontend can
# ask for it and mean it, not so a screen goes dark without it. Enforcing it
# anywhere is a decision for later, made once, not implicitly by whichever
# endpoint happens to check first.


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class VerifyEmailResponse(BaseModel):
    #: So the frontend can offer "sign in to <organisation>" — the browser
    #: completing this is often not the one that is actually signed in (the
    #: link was opened on a phone, say).
    tenant_slug: str


@router.post("/verify-email", response_model=VerifyEmailResponse)
async def verify_email(payload: VerifyEmailRequest) -> VerifyEmailResponse:
    """Spend a signup confirmation link.

    Unauthenticated on purpose, like the password-reset endpoints: the token
    alone proves the address, which is the entire point, and demanding a
    session on top would fail for anyone who opened the link on a device they
    are not signed in on.
    """
    async with get_sessionmaker()() as db:
        user = await reset_tokens.consume(
            db, payload.token, purpose=reset_tokens.EMAIL_VERIFICATION
        )
        if user is None:
            # One message for expired, spent, forged and unknown alike — same
            # reasoning as the password-reset endpoint.
            raise ValidationError(
                "This confirmation link is no longer valid. Sign in and ask "
                "for a new one from your account page."
            )

        now = datetime.now(UTC)
        with bypass_tenant_scope():
            if user.email_verified_at is None:
                user.email_verified_at = now
            tenant = await db.get(Tenant, user.tenant_id)
        assert tenant is not None
        # The address is the person's, not the organisation's.
        account = await db.get(Account, user.account_id) if user.account_id else None
        if account is not None and account.email_verified_at is None:
            account.email_verified_at = now
        await db.flush()

        from suliko.core.audit import record

        await record(
            db,
            None,
            action="user.email_verified",
            entity_type="user",
            entity_id=user.id,
            tenant_id=user.tenant_id,
        )
        await db.commit()

    log.info("email_verified", user_id=user.id, tenant_id=user.tenant_id)
    return VerifyEmailResponse(tenant_slug=tenant.slug)


@router.post("/verify-email/resend", status_code=status.HTTP_204_NO_CONTENT)
async def resend_verification_email(
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
    db: Db,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    """Mail a fresh confirmation link to the signed-in user's own address.

    Never to an address the caller supplies — there is only "your own", which
    is what makes this safe to leave unauthenticated-adjacent but still gate
    on a session: no form field here is a way to make us mail a stranger.

    A no-op, not an error, once already verified: the banner that offers this
    button stops rendering at that point, but a stale tab or a double click
    must not burn a rate-limit slot on nothing.
    """
    user = await db.get(User, session.user_id)
    if user is None:
        raise AuthenticationError("Your account is no longer available.")
    # Nothing to confirm for someone who signs in with a phone number.
    if session.email_verified or not user.email:
        return

    account_key = f"emailverify:user:{user.id}"
    if retry := await limiter.check_email_verification_resend(account_key):
        raise RateLimitedError("Too many requests. Try again later.", retry_after=retry)
    await limiter.record_email_verification_resend(account_key)

    settings = get_settings()
    token = await reset_tokens.issue(
        db,
        user,
        ttl_seconds=settings.email_verification_ttl_hours * 3600,
        purpose=reset_tokens.EMAIL_VERIFICATION,
    )
    tenant = await db.get(Tenant, user.tenant_id)
    assert tenant is not None
    link = (
        f"{settings.app_url.rstrip('/')}/{tenant.locale}/verify-email?token={quote(token, safe='')}"
    )
    subject, body = _verification_email(user, tenant, link, settings.email_verification_ttl_hours)
    # Inline, not backgrounded: this endpoint IS the "send it" action — there
    # is no larger response the person is waiting on behind it, unlike signup.
    await mail.send(user.email, subject, body)


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
    email: str = Field(min_length=3, max_length=255)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


def _reset_email(account: Account, link: str, ttl_minutes: int) -> tuple[str, str]:
    """Subject and plain-text body. No HTML — see core/mail.py."""
    hours = ttl_minutes // 60
    validity = f"{hours} hour{'s' if hours != 1 else ''}" if hours else f"{ttl_minutes} minutes"
    body = (
        f"Hello {account.full_name or account.email},\n\n"
        f"Someone asked to reset the password for your Suliko account "
        f"({account.email}).\n\n"
        f"Open this link to choose a new one:\n\n{link}\n\n"
        f"The link works once and expires in {validity}. The new password "
        f"is the one you sign in with everywhere, in every organisation you "
        f"belong to.\n\n"
        f"If this wasn't you, you can ignore this email. Your password has "
        f"not changed. Nobody can use this link without opening it.\n"
    )
    return "Reset your Suliko password", body


def _suliko_password_email(account: Account, reset_url: str) -> tuple[str, str]:
    """For someone whose password lives on suliko.ge: where to reset it."""
    body = (
        f"Hello {account.full_name or login_name(account)},\n\n"
        "Someone asked to reset the password for your Suliko Office account "
        f"({login_name(account)}).\n\n"
        "Your password is kept on suliko.ge, and Suliko Office signs you in with it, "
        "so it is changed there. Reset it here:\n\n"
        f"{reset_url}\n\n"
        "Then sign in to Suliko Office with the new password.\n\n"
        "No suliko.ge account yet? Register with exactly this email address, "
        f"then sign in here:\n\n{registration_url()}\n\n"
        "If this wasn't you, you can ignore this email. Nothing has changed.\n"
    )
    return "Reset your Suliko password", body


@router.post("/password/forgot", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    background: BackgroundTasks,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> None:
    """Send a reset link, if there is anywhere to send it.

    Answers 204 whether or not the account exists, and does the same amount of
    work either way. Anything else — a different status, a different message,
    a visibly different latency — turns this into a free tool for discovering
    which addresses are registered.

    The 429 is the one exception, and it leaks nothing: it is keyed on the
    address the caller just typed, which they already know.
    """
    ip = get_client_ip(request)
    email = normalise_email(payload.email)

    account_key = f"pwreset:acct:{email}"
    ip_key = f"pwreset:ip:{ip or 'unknown'}"

    if retry := await limiter.check_password_reset(account_key, ip_key):
        raise RateLimitedError("Too many reset requests. Try again later.", retry_after=retry)
    await limiter.record_password_reset_request(account_key, ip_key)

    settings = get_settings()

    async with get_sessionmaker()() as db:
        account = await find_account(db, email)
        if account is not None and (
            account.suliko_user_id is not None or account.password_hash == UNUSABLE_PASSWORD_HASH
        ):
            # Their password is on suliko.ge, so no link of ours could change
            # it — or they are an invitee who has not registered there yet, and
            # a link of ours would hand them a password that exists only here.
            # They are told where to go instead — after the response, like the
            # mail below, so this costs the same as the path that issues a token.
            subject, body = _suliko_password_email(account, settings.suliko_password_reset_url)
            background.add_task(mail.send, email, subject, body)
            return
        # The token is the reset-token machinery's, which is per membership
        # row; the one used most recently carries it. Which row it is changes
        # nothing — the password it sets is the account's.
        user: User | None = None
        tenant: Tenant | None = None
        if account is not None:
            with bypass_tenant_scope():
                row = (
                    await db.execute(
                        select(User, Tenant)
                        .join(Tenant, Tenant.id == User.tenant_id)
                        .where(User.account_id == account.id, User.is_active.is_(True))
                        .order_by(User.last_login_at.desc().nulls_last(), User.id.desc())
                        .limit(1)
                    )
                ).first()
            if row is not None:
                user, tenant = row

        if account is None or user is None or tenant is None:
            # Deliberately silent. Logged so an operator can see that someone
            # is trying, without the caller learning anything.
            log.info("password_reset_requested_for_unknown_account", ip=ip)
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
    subject, body = _reset_email(account, link, settings.password_reset_ttl_minutes)
    # After the response, not before it. Sending inline made a known account
    # take seconds (SMTP handshake, TLS, login) and an unknown one return at
    # once — the response time alone told anyone which addresses exist.
    background.add_task(mail.send, email, subject, body)


@router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(payload: ResetPasswordRequest, request: Request) -> None:
    """Spend a reset (or invitation) link and set the account's password.

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
        account = await db.get(Account, user.account_id) if user and user.account_id else None
        if user is None or account is None:
            # One message for expired, spent, forged and unknown alike.
            raise ValidationError(
                "This reset link is no longer valid. Request a new one and "
                "use the most recent email."
            )
        if account.suliko_user_id is not None:
            # The link was issued before this person's account became a
            # suliko.ge one (an invitation sent ahead of their registering
            # there). Their password is not ours to set any more.
            raise PasswordManagedExternallyError(
                "Your password is kept on suliko.ge. Reset it there, then sign in here."
            )

        now = datetime.now(UTC)
        account.password_hash = await hash_password_async(payload.password)
        # They chose this one themselves, proving they hold the mailbox.
        account.must_change_password = False
        if account.email_verified_at is None:
            account.email_verified_at = now
        with bypass_tenant_scope():
            if user.email_verified_at is None:
                user.email_verified_at = now
            # An invitation's set-password link arrives this way: using it is
            # accepting the invitation it was sent with.
            user.invitation_pending = False
        await db.flush()
        await revoke_account_sessions(db, account.id)

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

    log.info("password_reset_completed", account_id=account.id, user_id=user.id)


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: ChangePasswordRequest,
    session: Annotated[AuthenticatedSession, Depends(get_session_for_password_change)],
    db: Db,
) -> None:
    """Change your own password, proving you know the current one.

    Deliberately NOT behind `get_authenticated_session`. Someone holding a
    one-time password from an invite is refused by that gate everywhere else,
    and this is the screen they are being sent to.

    It is the account's password, so every session in every organisation is
    revoked, the caller's included — "changed everywhere" is what a person
    believes has happened, and keeping one session alive would make that wrong.
    """
    account = await db.get(Account, session.account_id) if session.account_id else None
    if account is None:
        raise AuthenticationError("Your account is no longer available.")
    if account.suliko_user_id is not None:
        # Nothing here to change: the password is suliko.ge's.
        raise PasswordManagedExternallyError(
            "Your password is kept on suliko.ge. Change it there, then sign in here."
        )

    ok, _ = await verify_and_maybe_rehash_async(payload.current_password, account.password_hash)
    if not ok:
        # 422 rather than 401: the session is perfectly valid, one field is
        # wrong. A 401 here would log the user out of the UI mid-form.
        raise ValidationError("Your current password is not correct.")

    if payload.new_password == payload.current_password:
        raise ValidationError("The new password must be different from the current one.")

    problems = validate_password_strength(payload.new_password)
    if problems:
        raise ValidationError(" ".join(problems))

    account.password_hash = await hash_password_async(payload.new_password)
    # Whatever they were handed, they have now replaced.
    account.must_change_password = False
    await db.flush()
    await revoke_account_sessions(db, account.id)

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="user.password_changed",
        entity_type="user",
        entity_id=session.user_id,
    )


# ── Invitations ─────────────────────────────────────────────────────────────
#
# An organisation adds a person who already has an account by inviting them;
# the membership stays pending — invisible to sign-in — until they accept from
# the email. Someone without an account gets a set-password link instead, and
# using it both creates their password and accepts (see `reset_password`).


class AcceptInvitationRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class AcceptInvitationResponse(BaseModel):
    tenant_slug: str
    tenant_name: str


@router.post("/invitations/accept", response_model=AcceptInvitationResponse)
async def accept_invitation(payload: AcceptInvitationRequest) -> AcceptInvitationResponse:
    """Spend an invitation link. Anonymous, like the reset link: the token
    alone proves the mailbox, and the person signs in afterwards."""
    async with get_sessionmaker()() as db:
        user = await reset_tokens.consume(db, payload.token, purpose=reset_tokens.INVITATION)
        if user is None:
            raise ValidationError(
                "This invitation link is no longer valid. Ask the organisation to invite you again."
            )
        with bypass_tenant_scope():
            user.invitation_pending = False
            tenant = await db.get(Tenant, user.tenant_id)
        assert tenant is not None
        await db.flush()

        from suliko.core.audit import record

        await record(
            db,
            None,
            action="user.invitation_accepted",
            entity_type="user",
            entity_id=user.id,
            tenant_id=user.tenant_id,
        )
        await db.commit()
    return AcceptInvitationResponse(tenant_slug=tenant.slug, tenant_name=tenant.display_name)


class SessionInfo(BaseModel):
    user_id: int
    username: str
    full_name: str
    #: Null for someone who signs in with a phone number.
    email: str | None
    #: What they sign in with: the address, else the phone.
    login: str
    #: The password is kept on suliko.ge: Office cannot change it, and the
    #: account page says where to.
    password_managed_externally: bool = False
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
    #: IANA zone for formatting dates and deciding "today" on screen.
    timezone: str
    #: Whether this user has a confirmed second factor (Account screen).
    has_mfa: bool
    #: Whether this address has been confirmed — drives the banner. Not
    #: enforced anywhere yet; see the note above `POST /auth/verify-email`.
    email_verified: bool
    #: For the organisation switcher: every other place this person can
    #: enter, and their personal workspace if it exists (None offers to
    #: create it). The current organisation is among them.
    organizations: list[OrgChoice] = Field(default_factory=list)
    personal: OrgChoice | None = None


@router.get("/session", response_model=SessionInfo)
async def current_session(
    session: Annotated[AuthenticatedSession, Depends(get_current_session)],
) -> SessionInfo:
    """What the BFF calls on every page load to hydrate the shell."""
    organizations: list[OrgChoice] = []
    own: OrgChoice | None = None
    login = session.email or session.username
    managed_externally = False
    if session.account_id is not None:
        async with get_sessionmaker()() as db:
            organizations, own = await _choices(db, session.account_id)
            account = await db.get(Account, session.account_id)
            if account is not None:
                login = login_name(account) or login
                managed_externally = account.suliko_user_id is not None
    return SessionInfo(
        organizations=organizations,
        personal=own,
        user_id=session.user_id,
        username=session.username,
        full_name=session.full_name,
        email=session.email,
        login=login,
        password_managed_externally=managed_externally,
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
        timezone=session.timezone,
        has_mfa=session.has_mfa,
        email_verified=session.email_verified,
    )


__all__ = ["resolve_session", "router"]
