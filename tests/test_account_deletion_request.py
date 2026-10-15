"""Asking for an account to be deleted (``POST /auth/deletion-request``).

Run for real against in-memory SQLite, like ``test_leave_organisation.py``.
What this cannot show is PostgreSQL itself accepting the migration's DDL and the
partial unique index; ``test_migration_0029.py`` pins what the migration says.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from suliko import cli
from suliko.api.v1 import auth
from suliko.config import get_settings
from suliko.core import mail
from suliko.core.errors import AuthenticationError, ConflictError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.account_deletion import (
    DELETION_GRACE_DAYS,
    AccountDeletionPendingError,
    DeletionAlreadyRequestedError,
    ensure_not_pending_deletion,
    open_request,
    open_requests,
    person_email,
    reject_request,
    team_email,
)
from suliko.domain.plans import TenantPlan, effective_permissions
from suliko.models.collaboration import Notification, OrderCommentMention, OrderCommentRead
from suliko.models.finance import ClientPaymentAllocation, NotaryPayment, TranslatorPayment
from suliko.models.order import Order, OrderDocument, OrderStatusEvent
from suliko.models.portal import (
    PersonalOrder,
    PersonalOrderFile,
    PersonalOrderLanguagePair,
    PortalAccountInvite,
    PortalTranslator,
    PortalTranslatorLink,
)
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import (
    Account,
    AccountDeletionRequest,
    DeletionStatus,
    MfaMethod,
    MfaRecoveryCode,
    PasswordResetToken,
    Role,
    User,
    UserPermissionOverride,
)
from suliko.security import login_tickets
from suliko.security.passwords import hash_password
from suliko.security.sessions import AuthenticatedSession

PASSWORD = "correct horse battery"
ACME, BETA = 1, 2
NINO, GIORGI = 1, 2
#: Everything a person's data can be in. `user_sessions` and `login_attempts` have
#: INET columns SQLite cannot create, so `_build` gives them just the columns the
#: code under test reads, as plain tables.
TABLES = [
    Tenant.__table__,
    Account.__table__,
    AccountDeletionRequest.__table__,
    User.__table__,
    MfaMethod.__table__,
    MfaRecoveryCode.__table__,
    PasswordResetToken.__table__,
    UserPermissionOverride.__table__,
    Notification.__table__,
    OrderCommentMention.__table__,
    OrderCommentRead.__table__,
    PortalTranslator.__table__,
    PortalTranslatorLink.__table__,
    PortalAccountInvite.__table__,
    PersonalOrder.__table__,
    PersonalOrderFile.__table__,
    PersonalOrderLanguagePair.__table__,
    Order.__table__,
    OrderDocument.__table__,
    OrderStatusEvent.__table__,
    ClientPaymentAllocation.__table__,
    TranslatorPayment.__table__,
    NotaryPayment.__table__,
]


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def record(_db: object, _session: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("suliko.core.audit.record", record)
    return calls


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/deletion-request",
            "headers": [],
            "client": ("203.0.113.9", 4321),
        }
    )


def _session(
    user_id: int,
    role: Role = Role.STAFF,
    *,
    account_id: int | None = NINO,
    tenant_id: int = ACME,
) -> AuthenticatedSession:
    session = AuthenticatedSession(
        session_id=1,
        user_id=user_id,
        username=f"u{user_id}",
        full_name=f"User {user_id}",
        email=f"u{user_id}@acme.ge",
        role=role,
        tenant_id=tenant_id,
        tenant_slug="acme" if tenant_id == ACME else "beta",
        tenant_name="Acme" if tenant_id == ACME else "Beta",
        plan=TenantPlan.BUREAU,
        onboarding_required=False,
        must_change_password=False,
        has_mfa=False,
        permissions=effective_permissions(role, TenantPlan.BUREAU),
        mfa_satisfied_at=None,
        impersonated_by_user_id=None,
    )
    return replace(session, account_id=account_id)


#: (id, tenant, account, role, active)
Member = tuple[int, int, int | None, Role, bool]

#: Nino is in both organisations; Giorgi, who must stay untouched, is only in Acme.
DEFAULT_MEMBERS: list[Member] = [
    (1, ACME, NINO, Role.STAFF, True),
    (2, BETA, NINO, Role.STAFF, True),
    (3, ACME, GIORGI, Role.OWNER, True),
]


async def _build(
    members: list[Member] | None = None,
    *,
    suliko_linked: bool = False,
    email: str | None = "nino@acme.ge",
) -> tuple[Any, AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
        await conn.execute(
            text("CREATE TABLE user_sessions (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)")
        )
        await conn.execute(
            text("CREATE TABLE login_attempts (id INTEGER PRIMARY KEY, username VARCHAR(100))")
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    db = maker()
    with bypass_tenant_scope():
        for tenant_id, slug in ((ACME, "acme"), (BETA, "beta")):
            db.add(
                Tenant(
                    id=tenant_id,
                    slug=slug,
                    display_name=slug.title(),
                    status=TenantStatus.ACTIVE,
                    plan="bureau",
                    locale="ka",
                )
            )
        db.add(
            Account(
                id=NINO,
                email=email,
                phone=None if email else "599123456",
                suliko_user_id="suliko-nino" if suliko_linked else None,
                full_name="Nino Beridze",
                password_hash="!" if suliko_linked else hash_password(PASSWORD),
            )
        )
        db.add(Account(id=GIORGI, email="giorgi@acme.ge", full_name="Giorgi", password_hash="!"))
        await db.flush()
        for user_id, tenant_id, account_id, role, active in members or DEFAULT_MEMBERS:
            db.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    account_id=account_id,
                    username=f"u{user_id}",
                    email=f"u{user_id}@acme.ge",
                    full_name=f"User {user_id}",
                    password_hash="!",
                    role=role,
                    is_active=active,
                )
            )
        await db.commit()
    return engine, db


@pytest_asyncio.fixture
async def world() -> AsyncIterator[Any]:
    """`world(members, ...)` builds a database and returns its session."""
    opened: list[tuple[Any, AsyncSession]] = []

    async def make(members: list[Member] | None = None, **options: Any) -> AsyncSession:
        engine, db = await _build(members, **options)
        opened.append((engine, db))
        return db

    yield make
    for engine, db in opened:
        await db.close()
        await engine.dispose()


async def ask(
    db: AsyncSession,
    session: AuthenticatedSession | None = None,
    password: str | None = PASSWORD,
) -> tuple[Any, BackgroundTasks]:
    """Run the handler the way a request does, and commit what it left."""
    background = BackgroundTasks()
    with tenant_scope(ACME):
        try:
            response = await auth.request_account_deletion(
                auth.DeletionRequestPayload(current_password=password),
                _request(),
                session or _session(1),
                db,
                background,
            )
        finally:
            await db.commit()
    return response, background


async def requests_of(db: AsyncSession, account_id: int = NINO) -> list[AccountDeletionRequest]:
    rows = await db.execute(
        select(AccountDeletionRequest)
        .where(AccountDeletionRequest.account_id == account_id)
        .order_by(AccountDeletionRequest.id)
    )
    return list(rows.scalars())


async def closed_at(db: AsyncSession, user_id: int) -> datetime | None:
    with bypass_tenant_scope():
        row = await db.get(User, user_id)
        assert row is not None
        await db.refresh(row)
        return row.sessions_invalid_before


async def is_active(db: AsyncSession, user_id: int) -> bool:
    with bypass_tenant_scope():
        row = await db.get(User, user_id)
        assert row is not None
        await db.refresh(row)
        return row.is_active


# ── Filing the request ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_asking_files_a_request_and_ends_every_session(world: Any) -> None:
    db = await world()
    response, _ = await ask(db)

    assert response.status == "requested"
    assert response.requested_at is not None
    (request,) = await requests_of(db)
    assert request.status is DeletionStatus.REQUESTED
    # Both of Nino's organisations: the one asked from and the other one.
    assert await closed_at(db, 1) is not None
    assert await closed_at(db, 2) is not None


@pytest.mark.asyncio
async def test_the_memberships_themselves_are_left_as_they_were(world: Any) -> None:
    """Closing the account is not switching its memberships off: that is what
    lets a cancelled request leave everything as it was."""
    db = await world()
    await ask(db)
    assert await is_active(db, 1) is True
    assert await is_active(db, 2) is True


@pytest.mark.asyncio
async def test_nobody_else_is_signed_out(world: Any) -> None:
    db = await world()
    await ask(db)
    assert await closed_at(db, 3) is None


@pytest.mark.asyncio
async def test_an_account_with_a_password_must_give_it(world: Any) -> None:
    db = await world()
    for password in (None, "wrong"):
        with pytest.raises(ValidationError):
            await ask(db, password=password)
    assert await requests_of(db) == []
    assert await closed_at(db, 1) is None


@pytest.mark.asyncio
async def test_a_suliko_account_has_no_password_here_to_ask_for(world: Any) -> None:
    db = await world(suliko_linked=True)
    response, _ = await ask(db, password=None)
    assert response.status == "requested"


@pytest.mark.asyncio
async def test_a_session_without_an_account_is_refused(world: Any) -> None:
    db = await world()
    with pytest.raises(AuthenticationError):
        await ask(db, _session(1, account_id=None))
    assert await requests_of(db) == []


@pytest.mark.asyncio
async def test_it_is_refused_while_acting_as_someone_else(world: Any) -> None:
    db = await world()
    with pytest.raises(ValidationError):
        await ask(db, replace(_session(1), impersonated_by_user_id=99))
    assert await requests_of(db) == []


# ── The last owner ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_last_owner_of_an_organisation_in_use_must_hand_it_over(world: Any) -> None:
    db = await world(
        [
            (1, ACME, NINO, Role.OWNER, True),
            (2, ACME, GIORGI, Role.STAFF, True),
        ]
    )
    with pytest.raises(ConflictError) as raised:
        await ask(db, _session(1, Role.OWNER))
    assert "Acme" in raised.value.detail
    assert raised.value.extra["organisations"] == ["Acme"]
    assert await requests_of(db) == []
    assert await closed_at(db, 1) is None


@pytest.mark.asyncio
async def test_every_organisation_that_would_be_left_is_named(world: Any) -> None:
    db = await world(
        [
            (1, ACME, NINO, Role.OWNER, True),
            (2, BETA, NINO, Role.OWNER, True),
            (3, ACME, GIORGI, Role.STAFF, True),
            (4, BETA, GIORGI, Role.STAFF, True),
        ]
    )
    with pytest.raises(ConflictError) as raised:
        await ask(db, _session(1, Role.OWNER))
    assert raised.value.extra["organisations"] == ["Acme", "Beta"]


@pytest.mark.asyncio
async def test_the_other_organisation_may_not_be_the_one_asked_from(world: Any) -> None:
    """Asked from Acme, where Nino is only staff, but Beta would be left ownerless."""
    db = await world(
        [
            (1, ACME, NINO, Role.STAFF, True),
            (2, BETA, NINO, Role.OWNER, True),
            (3, BETA, GIORGI, Role.STAFF, True),
        ]
    )
    with pytest.raises(ConflictError) as raised:
        await ask(db)
    assert raised.value.extra["organisations"] == ["Beta"]


@pytest.mark.asyncio
async def test_a_sole_user_a_co_owner_and_a_non_owner_can_ask(world: Any) -> None:
    for members in (
        [(1, ACME, NINO, Role.OWNER, True)],  # a freelancer's own workspace
        [(1, ACME, NINO, Role.OWNER, True), (2, ACME, GIORGI, Role.OWNER, True)],
        [(1, ACME, NINO, Role.STAFF, True), (2, ACME, GIORGI, Role.OWNER, True)],
    ):
        db = await world(members)
        response, _ = await ask(db, _session(1, members[0][3]))
        assert response.status == "requested"


@pytest.mark.asyncio
async def test_people_already_switched_off_do_not_count_as_staying(world: Any) -> None:
    db = await world(
        [
            (1, ACME, NINO, Role.OWNER, True),
            (2, ACME, GIORGI, Role.STAFF, False),
        ]
    )
    response, _ = await ask(db, _session(1, Role.OWNER))
    assert response.status == "requested"


@pytest.mark.asyncio
async def test_an_owner_who_is_already_switched_off_blocks_nothing(world: Any) -> None:
    db = await world(
        [
            (1, ACME, NINO, Role.STAFF, True),
            (2, BETA, NINO, Role.OWNER, False),
            (3, BETA, GIORGI, Role.STAFF, True),
        ]
    )
    response, _ = await ask(db)
    assert response.status == "requested"


# ── Only one at a time ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_request_while_one_is_open_is_refused(world: Any) -> None:
    db = await world()
    await ask(db)
    with pytest.raises(DeletionAlreadyRequestedError):
        await ask(db)
    assert len(await requests_of(db)) == 1


@pytest.mark.asyncio
async def test_a_cancelled_request_does_not_stop_a_new_one(world: Any) -> None:
    db = await world()
    await ask(db)
    (first,) = await requests_of(db)
    await reject_request(db, first.id)
    await db.commit()

    response, _ = await ask(db)
    assert response.status == "requested"
    assert [r.status for r in await requests_of(db)] == [
        DeletionStatus.REJECTED,
        DeletionStatus.REQUESTED,
    ]


@pytest.mark.asyncio
async def test_the_database_itself_allows_one_open_request_per_account(world: Any) -> None:
    """When two tabs press the button at once, both pass the handler's check. The
    partial unique index is what stops the second."""
    db = await world()
    db.add(AccountDeletionRequest(account_id=NINO, status=DeletionStatus.REQUESTED))
    await db.flush()
    db.add(AccountDeletionRequest(account_id=NINO, status=DeletionStatus.APPROVED))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()

    # A closed one beside an open one is the normal history.
    db.add(AccountDeletionRequest(account_id=GIORGI, status=DeletionStatus.REJECTED))
    db.add(AccountDeletionRequest(account_id=GIORGI, status=DeletionStatus.DONE))
    db.add(AccountDeletionRequest(account_id=GIORGI, status=DeletionStatus.REQUESTED))
    await db.flush()


# ── Signing in while it is open ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_closed_account_cannot_sign_in_and_a_cancelled_one_can(world: Any) -> None:
    db = await world()
    await ensure_not_pending_deletion(db, NINO)  # nothing filed yet

    await ask(db)
    with pytest.raises(AccountDeletionPendingError) as raised:
        await ensure_not_pending_deletion(db, NINO)
    assert raised.value.status_code == 403
    assert raised.value.error_code == "account_deletion_pending"
    assert get_settings().support_email in raised.value.detail
    # Said in the body too, so the frontend can print the address itself.
    assert raised.value.extra["support_email"] == get_settings().support_email

    await ensure_not_pending_deletion(db, GIORGI)  # someone else is unaffected

    (request,) = await requests_of(db)
    await reject_request(db, request.id)
    await ensure_not_pending_deletion(db, NINO)


@pytest.mark.asyncio
async def test_an_approved_request_keeps_the_account_closed_too(world: Any) -> None:
    db = await world()
    db.add(AccountDeletionRequest(account_id=NINO, status=DeletionStatus.APPROVED))
    await db.flush()
    with pytest.raises(AccountDeletionPendingError):
        await ensure_not_pending_deletion(db, NINO)


@pytest.mark.asyncio
async def test_a_ticket_issued_before_the_request_is_no_good_after_it(world: Any) -> None:
    db = await world()
    account = await db.get(Account, NINO)
    assert account is not None
    ticket = login_tickets.issue(account.id, account.password_hash)

    assert (await auth._account_from_ticket(db, ticket)).id == NINO
    await ask(db)
    with pytest.raises(AccountDeletionPendingError):
        await auth._account_from_ticket(db, ticket)


@pytest.mark.asyncio
async def test_no_session_is_started_for_a_closed_account(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every way of getting a session (the chooser, the switcher, a new bureau)
    ends in `_start_session`, so this is where the rule is held for good."""
    from suliko.domain.accounts import memberships

    started: list[object] = []

    async def fake_session(*_args: object, **_kwargs: object) -> SimpleNamespace:
        started.append(1)
        return SimpleNamespace(token="t")

    monkeypatch.setattr(auth, "create_session", fake_session)

    db = await world()
    account = await db.get(Account, NINO)
    assert account is not None
    target = (await memberships(db, NINO))[0]

    await ask(db)
    with tenant_scope(ACME), pytest.raises(AccountDeletionPendingError):
        await auth._start_session(db, target, account, ip=None, user_agent=None)
    assert started == []


@pytest.mark.asyncio
async def test_a_refused_sign_in_is_logged_but_not_held_against_the_person(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[tuple[Any, ...]] = []

    async def record_attempt(_db: object, *args: Any) -> None:
        attempts.append(args)

    monkeypatch.setattr(auth, "_record_attempt", record_attempt)

    db = await world()
    account = await db.get(Account, NINO)
    assert account is not None

    # Nothing filed: the helper lets the sign-in through and writes nothing.
    await auth._refuse_if_deletion_pending(db, account, "nino@acme.ge", "203.0.113.9", "agent")
    assert attempts == []

    await ask(db)
    with pytest.raises(AccountDeletionPendingError):
        await auth._refuse_if_deletion_pending(db, account, "nino@acme.ge", "203.0.113.9", "agent")
    # (name, ip, user agent, succeeded, reason): a failure on the record, and no
    # rate-limiter call anywhere in that helper, so it is not counted against them.
    assert attempts == [("nino@acme.ge", "203.0.113.9", "agent", False, "deletion_pending")]


class _Limiter:
    """Stands in for the rate limiter, counting what is held against the person."""

    def __init__(self) -> None:
        self.failures = 0

    async def check_login(self, *_keys: str) -> None:
        return None

    async def record_login_failure(self, *_keys: str) -> None:
        self.failures += 1

    async def clear_login_failures(self, *_keys: str) -> None:
        return None


async def _sign_in_fixtures(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> tuple[Account, list[tuple[Any, ...]]]:
    """Point the endpoints at the test database, and catch the attempts they log."""
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    attempts: list[tuple[Any, ...]] = []

    async def record_attempt(_db: object, *args: Any) -> None:
        attempts.append(args)

    monkeypatch.setattr(auth, "get_sessionmaker", lambda: maker)
    monkeypatch.setattr(auth, "_record_attempt", record_attempt)
    account = await db.get(Account, NINO)
    assert account is not None
    return account, attempts


@pytest.mark.asyncio
async def test_a_password_sign_in_is_stopped_before_it_gets_a_ticket(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await world()
    account, attempts = await _sign_in_fixtures(db, monkeypatch)

    async def authenticate(*_args: object) -> tuple[Account, bool]:
        return account, False

    monkeypatch.setattr(auth, "_authenticate", authenticate)
    payload = auth.LoginRequest(identifier="nino@acme.ge", password="whatever")
    limiter = _Limiter()

    options = await auth.login(payload, _request(), limiter, SimpleNamespace())  # type: ignore[arg-type]
    assert options.ticket
    assert attempts[-1][3] is True

    await ask(db)
    with pytest.raises(AccountDeletionPendingError) as raised:
        await auth.login(payload, _request(), limiter, SimpleNamespace())  # type: ignore[arg-type]
    assert raised.value.error_code == "account_deletion_pending"
    assert attempts[-1][3:] == (False, "deletion_pending")
    assert limiter.failures == 0


@pytest.mark.asyncio
async def test_a_suliko_sign_in_is_stopped_before_it_gets_a_ticket(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await world(suliko_linked=True)
    account, attempts = await _sign_in_fixtures(db, monkeypatch)

    async def redeem(*_args: object) -> SimpleNamespace:
        return SimpleNamespace(id="suliko-nino")

    async def account_for(*_args: object) -> Account:
        return account

    backend = SimpleNamespace(redeem_sso_code=redeem)
    monkeypatch.setattr(auth, "_account_for_suliko_user", account_for)
    payload = auth.SsoExchangeRequest(
        code="one-time",
        code_verifier="v" * 43,
        redirect_uri="https://office.suliko.ge/api/auth/sso/callback",
    )

    options = await auth.sso_exchange(payload, _request(), _Limiter(), backend)  # type: ignore[arg-type]
    assert options.ticket

    await ask(db, password=None)
    with pytest.raises(AccountDeletionPendingError):
        await auth.sso_exchange(payload, _request(), _Limiter(), backend)  # type: ignore[arg-type]
    assert attempts[-1][3:] == (False, "deletion_pending")


# ── Telling people ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_it_is_audited(world: Any, audit: list[dict[str, Any]]) -> None:
    db = await world()
    await ask(db)
    (entry,) = audit
    assert entry["action"] == "account.deletion_requested"
    assert entry["entity_type"] == "account"
    assert entry["entity_id"] == NINO
    assert entry["after"]["status"] == "requested"
    assert entry["ip"] == "203.0.113.9"


@pytest.mark.asyncio
async def test_the_team_and_the_person_are_each_sent_a_note(world: Any) -> None:
    db = await world()
    _, background = await ask(db)

    assert [task.func for task in background.tasks] == [mail.send, mail.send]
    (team_to, team_subject, team_body), (person_to, _, person_body) = [
        (str(to), str(subject), str(body))
        for to, subject, body, _html in (t.args for t in background.tasks)
    ]

    assert team_to == get_settings().support_email == "info@suliko.ge"
    assert "Nino Beridze" in team_subject
    assert "nino@acme.ge" in team_body
    assert "Acme (acme)" in team_body
    (request,) = await requests_of(db)
    assert f"--id {request.id}" in team_body

    assert person_to == "nino@acme.ge"
    assert get_settings().support_email in person_body


@pytest.mark.asyncio
async def test_someone_with_no_address_still_tells_the_team(world: Any) -> None:
    """A phone-only suliko.ge account has nobody to confirm to by email."""
    db = await world(suliko_linked=True, email=None)
    _, background = await ask(db, password=None)

    assert [task.args[0] for task in background.tasks] == [get_settings().support_email]


def test_the_note_to_the_team_says_what_to_check_and_how_to_cancel() -> None:
    account = Account(
        id=7,
        email="nino@acme.ge",
        phone=None,
        suliko_user_id="abc-123",
        full_name="Nino Beridze",
        password_hash="!",
    )
    request = AccountDeletionRequest(
        id=41,
        account_id=7,
        status=DeletionStatus.REQUESTED,
        created_at=datetime(2026, 10, 15, 9, 30, tzinfo=UTC),
    )
    message = team_email(account, request, organisation="Acme", organisation_slug="acme")
    subject, body = message.subject, message.text

    assert subject == "Account deletion request: Nino Beridze"
    for expected in (
        "nino@acme.ge",
        "abc-123",
        "2026-10-15 09:30 UTC",
        "unpaid invoices",
        "payouts owed to a translator",
        "only owner",
        f"{DELETION_GRACE_DAYS} days",
        "cancel-deletion-request --id 41",
    ):
        assert expected in body, expected


def test_the_note_to_the_person_promises_only_what_the_flow_does() -> None:
    message = person_email(Account(id=7, email="n@x.ge", full_name="Nino", password_hash="!"))
    subject, body = message.subject, message.text
    assert "delete" in subject
    assert "Hello Nino," in body
    assert f"{DELETION_GRACE_DAYS} days" in body
    assert "Write to info@suliko.ge" in body
    assert "—" not in body  # no em dashes in Suliko copy


# ── Ending a request ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancelling_reopens_the_account_and_deletes_nothing(world: Any) -> None:
    db = await world()
    await ask(db)
    (request,) = await requests_of(db)

    rejected = await reject_request(db, request.id)
    assert rejected.status is DeletionStatus.REJECTED
    assert await open_request(db, NINO) is None
    assert await db.get(Account, NINO) is not None
    assert await is_active(db, 1) is True


@pytest.mark.asyncio
async def test_there_is_nothing_to_cancel_twice_or_for_an_unknown_number(world: Any) -> None:
    db = await world()
    await ask(db)
    (request,) = await requests_of(db)
    await reject_request(db, request.id)

    with pytest.raises(ValidationError, match="already rejected"):
        await reject_request(db, request.id)
    with pytest.raises(ValidationError, match="no deletion request"):
        await reject_request(db, 9999)


@pytest.mark.asyncio
async def test_the_open_requests_are_listed_oldest_first(world: Any) -> None:
    db = await world()
    db.add(AccountDeletionRequest(account_id=GIORGI, status=DeletionStatus.REQUESTED))
    await db.flush()
    db.add(AccountDeletionRequest(account_id=NINO, status=DeletionStatus.APPROVED))
    db.add(AccountDeletionRequest(account_id=NINO, status=DeletionStatus.DONE))
    await db.flush()

    listed = await open_requests(db)
    assert [item.account.id for item in listed] == [GIORGI, NINO]
    assert [item.request.status for item in listed] == [
        DeletionStatus.REQUESTED,
        DeletionStatus.APPROVED,
    ]


# ── The command the team runs to cancel ─────────────────────────────────────


@pytest.mark.asyncio
async def test_the_command_cancels_a_request_and_leaves_a_trail(
    world: Any, monkeypatch: pytest.MonkeyPatch, audit: list[dict[str, Any]]
) -> None:
    db = await world()
    await ask(db)
    (request,) = await requests_of(db)
    audit.clear()

    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(cli, "get_sessionmaker", lambda: maker)
    await cli.cancel_deletion_request(request.id)

    async with maker() as fresh:
        assert await open_request(fresh, NINO) is None
        (stored,) = await requests_of(fresh)
        assert stored.status is DeletionStatus.REJECTED
    (entry,) = audit
    assert entry["action"] == "account.deletion_cancelled"
    assert entry["entity_id"] == NINO


@pytest.mark.asyncio
async def test_the_command_says_so_plainly_when_there_is_nothing_to_cancel(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await world()
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(cli, "get_sessionmaker", lambda: maker)

    with pytest.raises(SystemExit, match="no deletion request 5"):
        await cli.cancel_deletion_request(5)


@pytest.mark.asyncio
async def test_the_listing_names_who_is_waiting(
    world: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = await world()
    await ask(db)
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(cli, "get_sessionmaker", lambda: maker)

    await cli.deletion_requests()
    shown = capsys.readouterr().err
    assert "Nino Beridze <nino@acme.ge>" in shown
    assert "cancel-deletion-request --id" in shown
