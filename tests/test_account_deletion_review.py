"""Reviewing account-deletion requests and deleting the person's data (C2.3).

Run for real against in-memory SQLite, sharing the world built in
``test_account_deletion_request.py``. What this cannot show is PostgreSQL itself
running the queries; they avoid anything PostgreSQL-only for that reason, and
money comes back as whole cents.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from suliko import cli
from suliko.core import mail
from suliko.core.errors import ValidationError
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter
from suliko.domain import account_deletion
from suliko.domain.account_deletion import (
    DELETED_NAME,
    DELETION_GRACE_DAYS,
    AccountDeletionPendingError,
    ObligationsOpenError,
    approve_request,
    approved_email,
    done_email,
    due_for_purge,
    ensure_not_pending_deletion,
    findings,
    open_requests,
    purge_account,
    recent_decided_requests,
    reject_request,
    rejected_email,
)
from suliko.domain.accounts import find_account_by_suliko_id
from suliko.models.collaboration import Notification, NotificationKind
from suliko.models.finance import ClientPaymentAllocation, NotaryPayment, TranslatorPayment
from suliko.models.order import Order, OrderDocument, OrderStatusEvent
from suliko.models.portal import (
    FileKind,
    InviteKind,
    PersonalOrder,
    PersonalOrderFile,
    PersonalOrderLanguagePair,
    PortalAccountInvite,
    PortalTranslator,
    PortalTranslatorLink,
)
from suliko.models.user import (
    Account,
    AccountDeletionRequest,
    DeletionStatus,
    MfaMethod,
    PasswordResetToken,
    Role,
    User,
)

# The world and its plain helpers are shared; the fixtures are redefined here so that
# each module reads on its own.
from test_account_deletion_request import (
    ACME,
    GIORGI,
    NINO,
    Member,
    _build,
    _session,
    ask,
    requests_of,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
LINKED = {"suliko_linked": True}


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


# ── Building data ───────────────────────────────────────────────────────────


async def add_order(
    db: AsyncSession,
    order_id: int,
    *,
    tenant: int = ACME,
    events: tuple[str, ...] = (),
    delivery: str = "0",
    documents: tuple[tuple[str, str, int | None], ...] = (),
    paid: str | None = None,
) -> None:
    """An order, its status history, its documents `(price, translator cost, translator)`
    and one client payment allocated to it."""
    with bypass_tenant_scope():
        db.add(
            Order(
                id=order_id,
                tenant_id=tenant,
                number=order_id,
                client_id=1,
                order_date=date(2026, 10, 1),
                delivery_cost=Decimal(delivery),
            )
        )
        for step, status in enumerate(events):
            db.add(
                OrderStatusEvent(
                    tenant_id=tenant,
                    order_id=order_id,
                    status=status,
                    changed_at=NOW - timedelta(days=10 - step),
                )
            )
        for price, cost, translator in documents:
            db.add(
                OrderDocument(
                    tenant_id=tenant,
                    order_id=order_id,
                    source_language="ka",
                    target_language="en",
                    price=Decimal(price),
                    translator_cost=Decimal(cost),
                    translator_id=translator,
                )
            )
        if paid is not None:
            db.add(
                ClientPaymentAllocation(
                    tenant_id=tenant,
                    payment_id=1,
                    order_id=order_id,
                    amount_allocated=Decimal(paid),
                )
            )
        await db.flush()


async def add_portal(db: AsyncSession, *, active: bool = True) -> None:
    """Nino as a translator of Acme's directory row 5, known to the portal."""
    with bypass_tenant_scope():
        db.add(
            PortalTranslator(
                id=1, external_user_id="suliko-nino", display_name="Nino", is_active=active
            )
        )
        await db.flush()
        db.add(PortalTranslatorLink(portal_translator_id=1, tenant_id=ACME, translator_id=5))
        await db.flush()


def codes(found: list[Any]) -> list[str]:
    return [f.code for f in found]


# ── What the reviewer sees ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_plain_member_has_nothing_to_weigh(world: Any) -> None:
    db = await world()
    assert await findings(db, NINO) == []


@pytest.mark.asyncio
async def test_a_workspace_only_they_use_shows_its_open_and_unpaid_orders(world: Any) -> None:
    db = await world([(1, ACME, NINO, Role.OWNER, True)])
    # Open and part paid: 100 less 40.
    await add_order(db, 1, events=(), documents=(("100.00", "0", None),), paid="40.00")
    # Delivered and settled.
    await add_order(db, 2, events=("completed",), documents=(("50.00", "0", None),), paid="50.00")
    # Cancelled: owed nothing, however unpaid.
    await add_order(db, 3, events=("new", "cancelled"), documents=(("30.00", "0", None),))
    # Its LATEST status counts: new, then delivered. Unpaid, with delivery on top.
    await add_order(
        db, 4, events=("new", "completed"), delivery="5.00", documents=(("10.00", "0", None),)
    )

    found = await findings(db, NINO)

    assert codes(found) == ["open_orders", "unpaid_orders"]
    opened, unpaid = found
    assert (opened.organisation, opened.count, opened.blocking) == ("Acme", 1, False)
    assert (unpaid.count, unpaid.amount) == (2, Decimal("75.00"))


@pytest.mark.asyncio
async def test_an_organisation_other_people_use_is_not_theirs_to_settle(world: Any) -> None:
    """A co-owner of a bureau with colleagues: its orders are the bureau's."""
    db = await world(
        [
            (1, ACME, NINO, Role.OWNER, True),
            (2, ACME, GIORGI, Role.OWNER, True),
        ]
    )
    await add_order(db, 1, documents=(("100.00", "0", None),))
    assert await findings(db, NINO) == []


@pytest.mark.asyncio
async def test_the_last_owner_of_a_bureau_in_use_is_a_blocking_finding(world: Any) -> None:
    db = await world(
        [
            (1, ACME, NINO, Role.OWNER, True),
            (2, ACME, GIORGI, Role.STAFF, True),
        ]
    )
    (finding,) = await findings(db, NINO)
    assert (finding.code, finding.organisation, finding.blocking) == ("only_owner", "Acme", True)


@pytest.mark.asyncio
async def test_what_a_bureau_still_owes_them_as_a_translator_is_shown(world: Any) -> None:
    db = await world(**LINKED)
    await add_portal(db)
    await add_order(db, 11, events=("completed",), documents=(("100", "80.00", 5),))
    await add_order(db, 12, documents=(("100", "20.00", 5),))
    await add_order(db, 13, events=("cancelled",), documents=(("100", "50.00", 5),))
    await add_order(db, 14, documents=(("100", "99.00", 6),))  # another translator
    with bypass_tenant_scope():
        db.add(
            TranslatorPayment(
                id=1, tenant_id=ACME, translator_id=5, amount=Decimal("60.00"), payment_date=NOW
            )
        )
        await db.flush()

    (finding,) = await findings(db, NINO)
    # Earned 80 + 20 on live orders, paid 60: 40 still owed. The cancelled order
    # and the other translator's are not theirs.
    assert (finding.code, finding.organisation, finding.amount) == (
        "payouts_owed",
        "Acme",
        Decimal("40.00"),
    )


@pytest.mark.asyncio
async def test_a_notary_fee_they_fronted_is_not_counted_as_paid_to_them(world: Any) -> None:
    db = await world(**LINKED)
    await add_portal(db)
    await add_order(db, 11, events=("completed",), documents=(("100", "100.00", 5),))
    with bypass_tenant_scope():
        db.add(
            TranslatorPayment(
                id=1, tenant_id=ACME, translator_id=5, amount=Decimal("60.00"), payment_date=NOW
            )
        )
        await db.flush()
        db.add(
            NotaryPayment(
                tenant_id=ACME,
                amount=Decimal("10.00"),
                payment_date=NOW,
                paid_via_translator_payment_id=1,
            )
        )
        await db.flush()

    (finding,) = await findings(db, NINO)
    # 60 paid out, 10 of it a notary's fee they advanced: 50 for them, so 50 owed.
    assert finding.amount == Decimal("50.00")


@pytest.mark.asyncio
async def test_a_translator_owed_nothing_has_no_finding(world: Any) -> None:
    db = await world(**LINKED)
    await add_portal(db)
    await add_order(db, 11, events=("completed",), documents=(("100", "60.00", 5),))
    with bypass_tenant_scope():
        db.add(
            TranslatorPayment(
                id=1, tenant_id=ACME, translator_id=5, amount=Decimal("60.00"), payment_date=NOW
            )
        )
        await db.flush()
    assert await findings(db, NINO) == []


# ── Approving ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approving_starts_the_waiting_time_and_records_who_and_why(world: Any) -> None:
    db = await world()
    await ask(db)
    (request,) = await requests_of(db)

    approved = await approve_request(
        db, request.id, decided_by_user_id=3, note="  Checked, nothing owed.  ", now=NOW
    )

    assert approved.status is DeletionStatus.APPROVED
    assert approved.decided_at == NOW
    assert approved.decided_by_user_id == 3
    assert approved.review_note == "Checked, nothing owed."
    assert approved.purge_after == NOW + timedelta(days=DELETION_GRACE_DAYS)


@pytest.mark.asyncio
async def test_an_approved_account_is_still_closed(world: Any) -> None:
    db = await world()
    await ask(db)
    (request,) = await requests_of(db)
    await approve_request(db, request.id, decided_by_user_id=3, now=NOW)
    with pytest.raises(AccountDeletionPendingError):
        await ensure_not_pending_deletion(db, NINO)


@pytest.mark.asyncio
async def test_approval_needs_the_reviewer_to_say_they_looked(world: Any) -> None:
    db = await world([(1, ACME, NINO, Role.OWNER, True)])
    await add_order(db, 1, documents=(("100.00", "0", None),))
    await ask(db, _session(1, Role.OWNER))
    (request,) = await requests_of(db)

    with pytest.raises(ObligationsOpenError) as raised:
        await approve_request(db, request.id, decided_by_user_id=3)
    assert raised.value.extra["blocking"] is False
    assert (await requests_of(db))[0].status is DeletionStatus.REQUESTED

    approved = await approve_request(db, request.id, decided_by_user_id=3, acknowledged=True)
    assert approved.status is DeletionStatus.APPROVED


@pytest.mark.asyncio
async def test_no_acknowledgement_gets_past_an_organisation_left_without_an_owner(
    world: Any,
) -> None:
    db = await world([(1, ACME, NINO, Role.STAFF, True), (2, ACME, GIORGI, Role.OWNER, True)])
    await ask(db)
    (request,) = await requests_of(db)
    # Between the request and the review the only owner was handed to Nino, and
    # the old owner left: the organisation is now Nino's alone to run.
    with bypass_tenant_scope():
        nino = await db.get(User, 1)
        giorgi = await db.get(User, 2)
        assert nino is not None and giorgi is not None
        nino.role = Role.OWNER
        giorgi.role = Role.STAFF
        await db.flush()

    with pytest.raises(ObligationsOpenError) as raised:
        await approve_request(db, request.id, decided_by_user_id=3, acknowledged=True)
    assert raised.value.extra["blocking"] is True
    assert "Acme" in raised.value.detail


@pytest.mark.asyncio
async def test_only_a_new_request_can_be_approved(world: Any) -> None:
    db = await world()
    await ask(db)
    (request,) = await requests_of(db)
    await approve_request(db, request.id, decided_by_user_id=3)
    with pytest.raises(ValidationError, match="only a new request"):
        await approve_request(db, request.id, decided_by_user_id=3)
    with pytest.raises(ValidationError, match="no deletion request"):
        await approve_request(db, 9999, decided_by_user_id=3)


# ── Rejecting ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejecting_records_the_decision_and_opens_the_account(world: Any) -> None:
    db = await world()
    await ask(db)
    (request,) = await requests_of(db)

    rejected = await reject_request(
        db, request.id, decided_by_user_id=3, note="Asked by mistake", now=NOW
    )

    assert rejected.status is DeletionStatus.REJECTED
    assert (rejected.decided_at, rejected.decided_by_user_id) == (NOW, 3)
    assert rejected.review_note == "Asked by mistake"
    await ensure_not_pending_deletion(db, NINO)


@pytest.mark.asyncio
async def test_an_approved_request_can_still_be_cancelled(world: Any) -> None:
    db = await world()
    await ask(db)
    (request,) = await requests_of(db)
    await approve_request(db, request.id, decided_by_user_id=3, now=NOW)

    rejected = await reject_request(db, request.id, decided_by_user_id=3)
    assert rejected.status is DeletionStatus.REJECTED
    assert rejected.purge_after is None  # nothing is waiting to be deleted any more
    await ensure_not_pending_deletion(db, NINO)


@pytest.mark.asyncio
async def test_the_portal_goes_off_with_the_request_and_back_on_with_a_cancel(world: Any) -> None:
    db = await world(**LINKED)
    await add_portal(db)

    await ask(db, password=None)
    (request,) = await requests_of(db)
    portal = await db.get(PortalTranslator, 1)
    assert portal is not None and portal.is_active is False
    assert request.portal_deactivated is True

    await reject_request(db, request.id)
    await db.refresh(portal)
    assert portal.is_active is True
    assert (await requests_of(db))[0].portal_deactivated is False


@pytest.mark.asyncio
async def test_a_portal_the_bureau_had_switched_off_itself_stays_off(world: Any) -> None:
    db = await world(**LINKED)
    await add_portal(db, active=False)

    await ask(db, password=None)
    (request,) = await requests_of(db)
    assert request.portal_deactivated is False

    await reject_request(db, request.id)
    portal = await db.get(PortalTranslator, 1)
    assert portal is not None
    await db.refresh(portal)
    assert portal.is_active is False


# ── Deleting ────────────────────────────────────────────────────────────────


async def approved_and_due(db: AsyncSession) -> AccountDeletionRequest:
    """Nino's request, filed, approved, and its waiting time over."""
    await ask(db, password=None)
    (request,) = await requests_of(db)
    return await approve_request(
        db, request.id, decided_by_user_id=3, now=NOW - timedelta(days=DELETION_GRACE_DAYS + 1)
    )


async def dirty_world(world: Any) -> AsyncSession:
    """Nino in two organisations, with everything a person leaves behind."""
    db = await world(**LINKED)
    await add_portal(db)
    await add_order(db, 1, events=("completed",), documents=(("100", "0", None),), paid="100.00")
    with bypass_tenant_scope():
        db.add(
            Notification(tenant_id=ACME, user_id=1, kind=next(iter(NotificationKind)), body="hi")
        )
        db.add(MfaMethod(tenant_id=ACME, user_id=1, method_type="totp", secret_encrypted=b"x"))
        db.add(
            PasswordResetToken(
                tenant_id=ACME,
                user_id=1,
                token_hash="a" * 64,
                expires_at=NOW + timedelta(days=1),
            )
        )
        db.add(
            PortalAccountInvite(
                tenant_id=ACME, kind=next(iter(InviteKind)), full_name="Nino", user_id=1
            )
        )
        db.add(PersonalOrder(id=1, portal_translator_id=1, client_name="Private client"))
        await db.flush()
        db.add(
            PersonalOrderLanguagePair(
                personal_order_id=1, source_language="ka", target_language="en"
            )
        )
        db.add(
            PersonalOrderFile(
                personal_order_id=1,
                kind=next(iter(FileKind)),
                file_name="scan.pdf",
                content_type="application/pdf",
                size_bytes=1,
                sha256="0" * 64,
                content=b"x",
            )
        )
        await db.execute(text("INSERT INTO user_sessions (user_id) VALUES (1), (2), (3)"))
        await db.execute(
            text(
                "INSERT INTO login_attempts (username) VALUES "
                "('nino@acme.ge'), ('NINO@acme.ge'), ('someone@else.ge')"
            )
        )
        await db.flush()
    return db


async def count(db: AsyncSession, model: Any) -> int:
    with bypass_tenant_scope():
        return len((await db.execute(select(model))).scalars().all())


async def scalar(db: AsyncSession, sql: str) -> Any:
    return (await db.execute(text(sql))).scalar_one()


@pytest.mark.asyncio
async def test_the_person_is_made_anonymous_everywhere(world: Any) -> None:
    db = await dirty_world(world)
    request = await approved_and_due(db)

    address, name = await purge_account(db, request, now=NOW)
    await db.commit()

    assert (address, name) == ("nino@acme.ge", "Nino Beridze")
    account = await db.get(Account, NINO)
    assert account is not None
    await db.refresh(account)
    assert (account.email, account.phone, account.suliko_user_id) == (None, None, None)
    assert account.full_name == DELETED_NAME
    assert account.password_hash == "!"  # matches no password

    with bypass_tenant_scope():
        mine = (await db.execute(select(User).where(User.account_id == NINO))).scalars().all()
    assert len(mine) == 2
    for user in mine:
        await db.refresh(user)
        assert (user.full_name, user.email, user.phone, user.position) == (
            DELETED_NAME,
            None,
            None,
            None,
        )
        assert user.username == f"deleted-{user.id}"
        assert user.is_active is False
        assert user.password_hash == "!"

    assert await count(db, Notification) == 0
    assert await count(db, MfaMethod) == 0
    assert await count(db, PasswordResetToken) == 0
    assert await count(db, PortalAccountInvite) == 0
    # Sessions of Nino's two memberships went; Giorgi's stays.
    assert await scalar(db, "SELECT group_concat(user_id) FROM user_sessions") == "3"
    # What they typed to sign in, in either case; someone else's attempt stays.
    assert (
        await scalar(db, "SELECT group_concat(username) FROM login_attempts") == "someone@else.ge"
    )

    (done,) = await requests_of(db)
    assert done.status is DeletionStatus.DONE
    assert done.done_at is not None


@pytest.mark.asyncio
async def test_other_people_and_the_organisations_records_are_left_alone(world: Any) -> None:
    db = await dirty_world(world)
    request = await approved_and_due(db)

    await purge_account(db, request, now=NOW)

    giorgi = await db.get(Account, GIORGI)
    assert giorgi is not None and giorgi.email == "giorgi@acme.ge"
    with bypass_tenant_scope():
        other = await db.get(User, 3)
    assert other is not None and other.full_name == "User 3" and other.is_active is True
    # The order and its payment belong to the organisation.
    assert await count(db, Order) == 1
    assert await count(db, ClientPaymentAllocation) == 1
    assert await count(db, OrderDocument) == 1


@pytest.mark.asyncio
async def test_the_portal_record_its_links_and_personal_orders_go(world: Any) -> None:
    db = await dirty_world(world)
    request = await approved_and_due(db)

    await purge_account(db, request, now=NOW)

    assert await count(db, PortalTranslator) == 0
    assert await count(db, PortalTranslatorLink) == 0
    assert await count(db, PersonalOrder) == 0
    assert await count(db, PersonalOrderFile) == 0
    assert await count(db, PersonalOrderLanguagePair) == 0


@pytest.mark.asyncio
async def test_a_later_suliko_sign_in_does_not_find_the_deleted_account(world: Any) -> None:
    """If it did, the person would be quietly brought back with a blank name."""
    db = await dirty_world(world)
    request = await approved_and_due(db)
    assert await find_account_by_suliko_id(db, "suliko-nino") is not None

    await purge_account(db, request, now=NOW)

    assert await find_account_by_suliko_id(db, "suliko-nino") is None


@pytest.mark.asyncio
async def test_a_deleted_account_is_no_longer_closed_but_cannot_be_used(world: Any) -> None:
    db = await dirty_world(world)
    request = await approved_and_due(db)
    await purge_account(db, request, now=NOW)
    # Over, not open: there is nothing left to keep closed, and nothing to sign in as.
    await ensure_not_pending_deletion(db, NINO)


@pytest.mark.asyncio
async def test_only_approved_requests_past_their_time_are_due(world: Any) -> None:
    db = await world()
    db.add(
        AccountDeletionRequest(
            account_id=NINO,
            status=DeletionStatus.APPROVED,
            purge_after=NOW - timedelta(days=1),
        )
    )
    db.add(
        AccountDeletionRequest(
            account_id=GIORGI,
            status=DeletionStatus.APPROVED,
            purge_after=NOW + timedelta(days=1),
        )
    )
    await db.flush()
    # Neither a waiting nor a cancelled request is ever due, whatever its date.
    db.add(AccountDeletionRequest(account_id=NINO, status=DeletionStatus.REJECTED, purge_after=NOW))
    await db.flush()

    due = await due_for_purge(db, NOW)
    assert [(request.account_id, request.status) for request in due] == [
        (NINO, DeletionStatus.APPROVED)
    ]


# ── The command ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_command_deletes_what_is_due_and_tells_the_person(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await dirty_world(world)
    await approved_and_due(db)
    await db.commit()
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(cli, "get_sessionmaker", lambda: maker)
    told: list[tuple[str, str, str]] = []

    async def send(to: str, subject: str, body: str, html: str | None = None) -> None:
        told.append((to, subject, body))

    monkeypatch.setattr(mail, "send", send)

    assert await cli.purge_accounts() == 0

    async with maker() as fresh:
        account = await fresh.get(Account, NINO)
        assert account is not None and account.email is None
        (request,) = await requests_of(fresh)
        assert request.status is DeletionStatus.DONE
    (to, subject, body) = told[0]
    assert to == "nino@acme.ge"
    assert "deleted" in subject
    assert "Hello Nino Beridze," in body

    # A second run has nothing left to do.
    told.clear()
    assert await cli.purge_accounts() == 0
    assert told == []


@pytest.mark.asyncio
async def test_one_failing_request_does_not_stop_the_others(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await world()
    for account_id in (NINO, GIORGI):
        db.add(
            AccountDeletionRequest(
                account_id=account_id,
                status=DeletionStatus.APPROVED,
                purge_after=NOW - timedelta(days=1),
            )
        )
    await db.commit()
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(cli, "get_sessionmaker", lambda: maker)
    real = account_deletion.purge_account
    calls: list[int] = []

    async def flaky(db: AsyncSession, request: AccountDeletionRequest, **kwargs: Any) -> Any:
        calls.append(request.account_id)
        if request.account_id == NINO:
            raise RuntimeError("boom")
        return await real(db, request, **kwargs)

    async def send(*_args: str) -> None:
        return None

    monkeypatch.setattr(cli, "purge_account", flaky)
    monkeypatch.setattr(mail, "send", send)

    assert await cli.purge_accounts() == 1  # the scheduler is told something failed
    assert sorted(calls) == [NINO, GIORGI]
    async with maker() as fresh:
        statuses = {
            r.account_id: r.status
            for r in (await fresh.execute(select(AccountDeletionRequest))).scalars()
        }
    assert statuses == {NINO: DeletionStatus.APPROVED, GIORGI: DeletionStatus.DONE}


# ── The lists ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_and_finished_requests_are_listed_apart(world: Any) -> None:
    db = await world()
    db.add(AccountDeletionRequest(account_id=NINO, status=DeletionStatus.REQUESTED))
    db.add(AccountDeletionRequest(account_id=GIORGI, status=DeletionStatus.DONE))
    await db.flush()

    assert [i.account.id for i in await open_requests(db)] == [NINO]
    assert [i.account.id for i in await recent_decided_requests(db)] == [GIORGI]


# ── What the person is told ─────────────────────────────────────────────────


def test_the_person_is_told_when_it_will_happen_and_how_to_stop_it() -> None:
    account = Account(id=7, email="n@x.ge", full_name="Nino", password_hash="!")
    request = AccountDeletionRequest(
        id=1,
        account_id=7,
        status=DeletionStatus.APPROVED,
        purge_after=datetime(2026, 11, 14, 12, 0, tzinfo=UTC),
    )
    message = approved_email(account, request)
    subject, body = message.subject, message.text
    assert "deleted" in subject
    assert "2026-11-14" in body
    assert "info@suliko.ge" in body
    assert "—" not in body
    assert "2026-11-14" in message.html


def test_the_person_is_told_the_request_ended_and_they_can_sign_in() -> None:
    message = rejected_email(Account(id=7, email="n@x.ge", full_name="Nino", password_hash="!"))
    subject, body = message.subject, message.text
    assert "open again" in subject
    assert "sign in again" in body
    assert "—" not in body


def test_the_person_is_told_it_is_done() -> None:
    message = done_email("Nino")
    subject, body = message.subject, message.text
    assert "has been deleted" in subject
    assert "Hello Nino," in body
    assert "—" not in body


# ── The endpoints a superuser uses ──────────────────────────────────────────


def _superuser() -> Any:
    return _session(3, Role.SUPERUSER)


@pytest.mark.asyncio
async def test_the_list_shows_who_is_waiting_and_what_to_look_at(world: Any) -> None:
    from suliko.api.v1 import platform

    db = await world([(1, ACME, NINO, Role.OWNER, True)])
    await add_order(db, 1, documents=(("100.00", "0", None),))
    await ask(db, _session(1, Role.OWNER))

    listed = await platform.list_deletion_requests(db, _superuser())

    (item,) = listed.open
    assert (item.name, item.login, item.status) == ("Nino Beridze", "nino@acme.ge", "requested")
    assert item.organisations == ["Acme"]
    assert [f.code for f in item.findings] == ["open_orders", "unpaid_orders"]
    assert listed.decided == []


@pytest.mark.asyncio
async def test_approving_over_the_api_audits_tells_the_person_and_answers_with_the_date(
    world: Any, audit: list[dict[str, Any]]
) -> None:
    from fastapi import BackgroundTasks

    from suliko.api.v1 import platform

    db = await world()
    await ask(db)
    (request,) = await requests_of(db)
    audit.clear()
    background = BackgroundTasks()

    out = await platform.approve_deletion_request(
        request.id,
        platform.DeletionApproval(note="Nothing owed."),
        db,
        _superuser(),
        background,
    )

    assert out.status == "approved"
    assert out.review_note == "Nothing owed."
    assert out.purge_after is not None
    assert out.findings == []  # decided: nothing left to weigh
    (entry,) = audit
    assert entry["action"] == "platform.deletion_approved"
    assert entry["entity_id"] == NINO
    (task,) = background.tasks
    assert task.func is mail.send
    assert task.args[0] == "nino@acme.ge"
    assert "will be deleted" in task.args[1]


@pytest.mark.asyncio
async def test_approving_over_the_api_refuses_what_has_not_been_looked_at(world: Any) -> None:
    from fastapi import BackgroundTasks

    from suliko.api.v1 import platform

    db = await world([(1, ACME, NINO, Role.OWNER, True)])
    await add_order(db, 1, documents=(("100.00", "0", None),))
    await ask(db, _session(1, Role.OWNER))
    (request,) = await requests_of(db)

    background = BackgroundTasks()
    with pytest.raises(ObligationsOpenError):
        await platform.approve_deletion_request(
            request.id, platform.DeletionApproval(), db, _superuser(), background
        )
    assert background.tasks == []  # nobody was told anything

    out = await platform.approve_deletion_request(
        request.id,
        platform.DeletionApproval(acknowledged=True),
        db,
        _superuser(),
        BackgroundTasks(),
    )
    assert out.status == "approved"


@pytest.mark.asyncio
async def test_rejecting_over_the_api_reopens_tells_the_person_and_audits(
    world: Any, audit: list[dict[str, Any]]
) -> None:
    from fastapi import BackgroundTasks

    from suliko.api.v1 import platform

    db = await world()
    await ask(db)
    (request,) = await requests_of(db)
    audit.clear()
    background = BackgroundTasks()

    out = await platform.reject_deletion_request(
        request.id, platform.DeletionRejection(note="They wrote."), db, _superuser(), background
    )

    assert out.status == "rejected"
    await ensure_not_pending_deletion(db, NINO)
    (entry,) = audit
    assert entry["action"] == "platform.deletion_rejected"
    (task,) = background.tasks
    assert task.args[0] == "nino@acme.ge"
    assert "open again" in task.args[1]


@pytest.mark.asyncio
async def test_a_request_that_does_not_exist_is_a_404(world: Any) -> None:
    from fastapi import BackgroundTasks

    from suliko.api.v1 import platform
    from suliko.core.errors import NotFoundError

    db = await world()
    for call in (
        platform.approve_deletion_request(
            99, platform.DeletionApproval(), db, _superuser(), BackgroundTasks()
        ),
        platform.reject_deletion_request(
            99, platform.DeletionRejection(), db, _superuser(), BackgroundTasks()
        ),
    ):
        with pytest.raises(NotFoundError):
            await call


def test_only_a_superuser_reaches_them() -> None:
    """The routes carry the same guard as the rest of the platform area."""
    from suliko.api.v1 import platform

    guarded = {
        route.path: route
        for route in platform.router.routes
        if "deletion-requests" in getattr(route, "path", "")
    }
    assert len(guarded) == 3
    for route in guarded.values():
        dependencies = {d.call for d in route.dependant.dependencies}  # type: ignore[attr-defined]
        assert any(
            getattr(call, "__qualname__", "").startswith("require.") for call in dependencies
        ), route.path
