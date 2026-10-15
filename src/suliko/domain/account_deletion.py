"""Asking for an account to be deleted, reviewing the request, and deleting.

"Delete account" on the Account page is a request, not an act (decided
2026-10-14): the team reviews it and confirms it only if the person has no
obligations, and 30 days after the confirmation the person's data is deleted.

* `file_request` records it, and the account is closed.
* While a request is open the account cannot sign in anywhere.
  `ensure_not_pending_deletion` is the one statement of that rule and every way of
  signing in calls it, so they cannot disagree. It is the ACCOUNT that is closed,
  not its memberships: they keep their own state, so ending the request leaves
  everything as it was. (The one thing besides sign-in is the translator portal,
  which does not use accounts: it is switched off and, if the request ends, back on.)
* `findings` is what a reviewer should look at before saying yes.
* `approve_request` starts the 30 days; `reject_request` ends a request without
  deleting anything (the review said no, or the person wrote to cancel), and
  reopens the account.
* `purge_account` is the deletion itself: the person's data is erased or made
  anonymous. Orders, clients and invoices stay with the organisation they belong
  to, as decided 2026-10-14.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, cast, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.config import get_settings
from suliko.core.errors import AppError, ConflictError, ValidationError
from suliko.core.mail_layout import Bold, Button, Email, Facts, Link, callout, compose, fine, p
from suliko.db.tenancy import bypass_tenant_scope
from suliko.domain.accounts import (
    UNUSABLE_PASSWORD_HASH,
    login_name,
    would_strand_organisation,
)
from suliko.domain.statuses import (
    CLOSED_STATUSES,
    EXCLUDED_FROM_AGGREGATES,
    INITIAL_STATUS,
    normalise,
)
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
from suliko.models.tenant import Tenant
from suliko.models.user import (
    OPEN_DELETION_STATUSES,
    Account,
    AccountDeletionRequest,
    DeletionStatus,
    LoginAttempt,
    MfaMethod,
    MfaRecoveryCode,
    PasswordResetToken,
    Role,
    User,
    UserPermissionOverride,
    UserSession,
)

#: How long after the team confirms a request the account is deleted. Said to the
#: person in the confirmation, and what `approve_request` sets `purge_after` by.
DELETION_GRACE_DAYS = 30

#: What a deleted person's name becomes, wherever it has to stay as a label.
DELETED_NAME = "Deleted user"


class AccountDeletionPendingError(AppError):
    """The person asked for this account to be deleted, and it is closed meanwhile.

    403 rather than 401: who they are is not in doubt, and the password was right.
    What is refused is the sign-in, until the request is cancelled.
    """

    status_code = 403
    error_code = "account_deletion_pending"


class DeletionAlreadyRequestedError(ConflictError):
    error_code = "deletion_already_requested"


async def open_request(db: AsyncSession, account_id: int) -> AccountDeletionRequest | None:
    """The request that is keeping this account closed, if there is one."""
    return (
        (
            await db.execute(
                select(AccountDeletionRequest).where(
                    AccountDeletionRequest.account_id == account_id,
                    AccountDeletionRequest.status.in_(OPEN_DELETION_STATUSES),
                )
            )
        )
        .scalars()
        .first()
    )


async def ensure_not_pending_deletion(db: AsyncSession, account_id: int) -> None:
    """Raise `AccountDeletionPendingError` if this account has asked to be deleted."""
    if await open_request(db, account_id) is not None:
        raise AccountDeletionPendingError(
            "This account is closed because its deletion was requested. "
            f"To cancel the request, write to {get_settings().support_email}.",
            support_email=get_settings().support_email,
        )


async def organisations_left_without_an_owner(db: AsyncSession, account_id: int) -> list[str]:
    """The organisations this person is the last active owner of, others still using them.

    Their names, for the message. Closing the account would leave each of those
    with nobody to run it, so they are handed over first — the same rule as
    closing your own access to one of them.
    """
    with bypass_tenant_scope():
        rows = (
            await db.execute(
                select(User, Tenant)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(
                    User.account_id == account_id,
                    User.role == Role.OWNER,
                    User.is_active.is_(True),
                    User.invitation_pending.is_(False),
                )
                .order_by(Tenant.display_name, User.id)
            )
        ).all()
    stranded: list[str] = []
    for user, tenant in rows:
        if await would_strand_organisation(db, user):
            stranded.append(tenant.display_name)
    return stranded


async def file_request(db: AsyncSession, account: Account) -> AccountDeletionRequest:
    """Record that this person asks for their account to be deleted."""
    if await open_request(db, account.id) is not None:
        raise DeletionAlreadyRequestedError(
            "You have already asked for this account to be deleted."
        )
    request = AccountDeletionRequest(account_id=account.id, status=DeletionStatus.REQUESTED)
    db.add(request)
    # The translator portal does not go through an account, so sign-in cannot
    # close it. Switch it off here, and remember that this is what did it.
    portal = await _portal_translator(db, account)
    if portal is not None and portal.is_active:
        portal.is_active = False
        request.portal_deactivated = True
    await db.flush()
    # `created_at` is the database's own clock; read it back rather than guess.
    await db.refresh(request)
    return request


async def _portal_translator(db: AsyncSession, account: Account) -> PortalTranslator | None:
    """The translator-portal record of this person, if suliko.ge knows them as one."""
    if not account.suliko_user_id:
        return None
    return (
        (
            await db.execute(
                select(PortalTranslator).where(
                    PortalTranslator.external_user_id == account.suliko_user_id
                )
            )
        )
        .scalars()
        .first()
    )


async def _request_or_error(db: AsyncSession, request_id: int) -> AccountDeletionRequest:
    request = await db.get(AccountDeletionRequest, request_id)
    if request is None:
        raise ValidationError(f"There is no deletion request {request_id}.")
    return request


def _clean_note(note: str | None) -> str | None:
    return (note or "").strip() or None


async def reject_request(
    db: AsyncSession,
    request_id: int,
    *,
    decided_by_user_id: int | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> AccountDeletionRequest:
    """End an open request without deleting anything: the account can sign in again.

    The review said no, or the person wrote to cancel. Whatever filing the
    request switched off (the translator portal) is switched back on.
    """
    request = await _request_or_error(db, request_id)
    if request.status not in OPEN_DELETION_STATUSES:
        raise ValidationError(
            f"Request {request_id} is already {request.status.value}; there is nothing to cancel."
        )
    request.status = DeletionStatus.REJECTED
    request.decided_at = now or datetime.now(UTC)
    request.decided_by_user_id = decided_by_user_id
    request.review_note = _clean_note(note)
    request.purge_after = None
    if request.portal_deactivated:
        account = await db.get(Account, request.account_id)
        portal = await _portal_translator(db, account) if account is not None else None
        if portal is not None:
            portal.is_active = True
        request.portal_deactivated = False
    await db.flush()
    return request


# ── What a reviewer should look at ─────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing about this person that bears on whether to say yes."""

    #: "only_owner", "open_orders", "unpaid_orders" or "payouts_owed".
    code: str
    organisation: str
    count: int | None = None
    amount: Decimal | None = None
    #: Saying yes would leave an organisation with nobody to run it: refused outright.
    blocking: bool = False


class ObligationsOpenError(ConflictError):
    """Approval refused: either it cannot be done, or nobody has looked yet."""

    error_code = "obligations_open"


_CHUNK = 500


def _chunks(ids: Sequence[int]) -> Iterator[list[int]]:
    for start in range(0, len(ids), _CHUNK):
        yield list(ids[start : start + _CHUNK])


def _cents(expression: Any) -> Any:
    """Money as whole cents, computed by the database.

    Whole numbers come back as plain integers on every database, so nothing here
    depends on how one stores a decimal. Never a float on the way to a total.
    """
    return cast(func.round(expression * 100), BigInteger)


def _money(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


async def _current_statuses(db: AsyncSession, order_ids: Sequence[int]) -> dict[int, str]:
    """Each order's status: its latest event. An order with none is new."""
    latest: dict[int, tuple[datetime, int, str]] = {}
    for chunk in _chunks(order_ids):
        rows = await db.execute(
            select(
                OrderStatusEvent.order_id,
                OrderStatusEvent.id,
                OrderStatusEvent.changed_at,
                OrderStatusEvent.status,
            ).where(OrderStatusEvent.order_id.in_(chunk))
        )
        for order_id, event_id, changed_at, status in rows:
            if order_id not in latest or (changed_at, event_id) > latest[order_id][:2]:
                latest[order_id] = (changed_at, event_id, status)
    return {order_id: normalise(entry[2]) for order_id, entry in latest.items()}


async def _cents_by(
    db: AsyncSession, key: Any, value: Any, ids: Sequence[int], *conditions: Any
) -> dict[int, int]:
    totals: dict[int, int] = defaultdict(int)
    for chunk in _chunks(ids):
        rows = await db.execute(
            select(key, _cents(func.coalesce(func.sum(value), 0)))
            .where(key.in_(chunk), *conditions)
            .group_by(key)
        )
        for row_key, cents in rows:
            totals[row_key] += int(cents)
    return totals


async def _workspace_findings(db: AsyncSession, tenant: Tenant) -> list[Finding]:
    """Open and unpaid orders in an organisation the person is the only user of."""
    orders = (
        await db.execute(
            select(Order.id, _cents(Order.delivery_cost)).where(Order.tenant_id == tenant.id)
        )
    ).all()
    ids = [order_id for order_id, _ in orders]
    if not ids:
        return []
    status = await _current_statuses(db, ids)

    def current(order_id: int) -> str:
        return status.get(order_id, INITIAL_STATUS)

    found: list[Finding] = []
    open_orders = sum(1 for order_id in ids if current(order_id) not in CLOSED_STATUSES)
    if open_orders:
        found.append(Finding("open_orders", tenant.display_name, count=open_orders))

    live = {
        order_id: int(delivery)
        for order_id, delivery in orders
        if current(order_id) not in EXCLUDED_FROM_AGGREGATES
    }
    documents = await _cents_by(db, OrderDocument.order_id, OrderDocument.price, list(live))
    paid = await _cents_by(
        db, ClientPaymentAllocation.order_id, ClientPaymentAllocation.amount_allocated, list(live)
    )
    owed = [
        delivery + documents.get(order_id, 0) - paid.get(order_id, 0)
        for order_id, delivery in live.items()
    ]
    unpaid = [cents for cents in owed if cents > 0]
    if unpaid:
        found.append(
            Finding(
                "unpaid_orders", tenant.display_name, count=len(unpaid), amount=_money(sum(unpaid))
            )
        )
    return found


async def _payout_findings(db: AsyncSession, account: Account) -> list[Finding]:
    """What each bureau still owes this person as a translator, by the bureau's own sums."""
    portal = await _portal_translator(db, account)
    if portal is None:
        return []
    links = (
        await db.execute(
            select(PortalTranslatorLink, Tenant)
            .join(Tenant, Tenant.id == PortalTranslatorLink.tenant_id)
            .where(PortalTranslatorLink.portal_translator_id == portal.id)
            .order_by(Tenant.display_name, PortalTranslatorLink.id)
        )
    ).all()
    found: list[Finding] = []
    for link, tenant in links:
        documents = (
            await db.execute(
                select(OrderDocument.order_id, _cents(OrderDocument.translator_cost)).where(
                    OrderDocument.tenant_id == link.tenant_id,
                    OrderDocument.translator_id == link.translator_id,
                )
            )
        ).all()
        status = await _current_statuses(db, sorted({order_id for order_id, _ in documents}))
        earned = sum(
            int(cents)
            for order_id, cents in documents
            if status.get(order_id, INITIAL_STATUS) not in EXCLUDED_FROM_AGGREGATES
        )
        payments = (
            await db.execute(
                select(TranslatorPayment.id, _cents(TranslatorPayment.amount)).where(
                    TranslatorPayment.tenant_id == link.tenant_id,
                    TranslatorPayment.translator_id == link.translator_id,
                )
            )
        ).all()
        # A notary fee the translator fronted is inside their payout: take it out,
        # as the bureau's own balance does, or it would count as their fee too.
        fronted = await _cents_by(
            db,
            NotaryPayment.paid_via_translator_payment_id,
            NotaryPayment.amount,
            [payment_id for payment_id, _ in payments],
        )
        paid = sum(int(cents) for _, cents in payments) - sum(fronted.values())
        if earned - paid > 0:
            found.append(Finding("payouts_owed", tenant.display_name, amount=_money(earned - paid)))
    return found


async def findings(db: AsyncSession, account_id: int) -> list[Finding]:
    """What a reviewer should look at before confirming this person's request.

    Checked here: an organisation they are the last owner of (blocks), open and
    unpaid orders in an organisation only they use, and what bureaus still owe
    them as a translator. NOT checked, because Office cannot see it: a balance
    on suliko.ge. The review screen says so.
    """
    account = await db.get(Account, account_id)
    if account is None:
        return []
    found: list[Finding] = []
    with bypass_tenant_scope():
        owned = (
            await db.execute(
                select(User, Tenant)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(
                    User.account_id == account_id,
                    User.role == Role.OWNER,
                    User.is_active.is_(True),
                    User.invitation_pending.is_(False),
                )
                .order_by(Tenant.display_name, User.id)
            )
        ).all()
        for user, tenant in owned:
            if await would_strand_organisation(db, user):
                found.append(Finding("only_owner", tenant.display_name, blocking=True))
                continue
            others = (
                await db.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(
                        User.tenant_id == tenant.id,
                        User.is_active.is_(True),
                        User.id != user.id,
                    )
                )
            ) or 0
            if others == 0:
                found.extend(await _workspace_findings(db, tenant))
        found.extend(await _payout_findings(db, account))
    return found


# ── Deciding ───────────────────────────────────────────────────────────────


async def approve_request(
    db: AsyncSession,
    request_id: int,
    *,
    decided_by_user_id: int,
    note: str | None = None,
    acknowledged: bool = False,
    now: datetime | None = None,
) -> AccountDeletionRequest:
    """Confirm a request: the person's data is deleted `DELETION_GRACE_DAYS` from now.

    Refused when an organisation would be left without an owner, whatever anyone
    says. Anything else the findings turned up needs `acknowledged`: the
    reviewer saying they looked, so a click cannot approve past them unseen.
    """
    request = await _request_or_error(db, request_id)
    if request.status is not DeletionStatus.REQUESTED:
        raise ValidationError(
            f"Request {request_id} is {request.status.value}; only a new request can be approved."
        )
    found = await findings(db, request.account_id)
    stranded = [f.organisation for f in found if f.blocking]
    if stranded:
        raise ObligationsOpenError(
            f"{', '.join(stranded)} would be left without an owner. "
            "Hand it over to someone first, then approve.",
            blocking=True,
        )
    if found and not acknowledged:
        raise ObligationsOpenError(
            "This person has things that may count as obligations. Look at them, "
            "then confirm that you have.",
            blocking=False,
        )
    moment = now or datetime.now(UTC)
    request.status = DeletionStatus.APPROVED
    request.decided_at = moment
    request.decided_by_user_id = decided_by_user_id
    request.review_note = _clean_note(note)
    request.purge_after = moment + timedelta(days=DELETION_GRACE_DAYS)
    await db.flush()
    return request


@dataclass(frozen=True, slots=True)
class OpenRequest:
    request: AccountDeletionRequest
    account: Account


async def open_requests(db: AsyncSession) -> list[OpenRequest]:
    """Every request keeping an account closed, oldest first."""
    rows = (
        await db.execute(
            select(AccountDeletionRequest, Account)
            .join(Account, Account.id == AccountDeletionRequest.account_id)
            .where(AccountDeletionRequest.status.in_(OPEN_DELETION_STATUSES))
            .order_by(AccountDeletionRequest.created_at, AccountDeletionRequest.id)
        )
    ).all()
    return [OpenRequest(request, account) for request, account in rows]


async def recent_decided_requests(db: AsyncSession, limit: int = 20) -> list[OpenRequest]:
    """The latest requests that are over (rejected or deleted), newest first."""
    rows = (
        await db.execute(
            select(AccountDeletionRequest, Account)
            .join(Account, Account.id == AccountDeletionRequest.account_id)
            .where(AccountDeletionRequest.status.not_in(OPEN_DELETION_STATUSES))
            .order_by(AccountDeletionRequest.updated_at.desc(), AccountDeletionRequest.id.desc())
            .limit(limit)
        )
    ).all()
    return [OpenRequest(request, account) for request, account in rows]


# ── Deleting ───────────────────────────────────────────────────────────────


async def due_for_purge(
    db: AsyncSession, now: datetime | None = None
) -> list[AccountDeletionRequest]:
    """Approved requests whose waiting time is over, the longest waiting first."""
    return list(
        (
            await db.execute(
                select(AccountDeletionRequest)
                .where(
                    AccountDeletionRequest.status == DeletionStatus.APPROVED,
                    AccountDeletionRequest.purge_after <= (now or datetime.now(UTC)),
                )
                .order_by(AccountDeletionRequest.purge_after, AccountDeletionRequest.id)
            )
        ).scalars()
    )


async def purge_account(
    db: AsyncSession, request: AccountDeletionRequest, *, now: datetime | None = None
) -> tuple[str | None, str]:
    """Delete the person's data. Returns the address and name they had, to tell them.

    The rows stay and lose who they were: the account and each membership are
    emptied of name, address, phone and password and can never sign in; sessions,
    second factors, reset links, notifications and mentions are deleted; the
    translator-portal record, its links and the person's own orders there go.
    Orders, clients, invoices and payments belong to the organisation and stay,
    their author shown as "Deleted user". The bureau's own directory entry for
    the person (a translator it paid) stays too: it is the bureau's accounting.

    An organisation the person alone owns is NOT deleted: that is a separate,
    explicit request (decided 2026-10-14).
    """
    moment = now or datetime.now(UTC)
    account = await db.get(Account, request.account_id)
    if account is None:
        raise ValidationError(f"Request {request.id} points at an account that is gone.")
    address, name, phone = account.email, account.full_name, account.phone

    with bypass_tenant_scope():
        users = list(
            (await db.execute(select(User).where(User.account_id == account.id))).scalars()
        )
        user_ids = [user.id for user in users]
        for user in users:
            user.full_name = DELETED_NAME
            user.username = f"deleted-{user.id}"
            user.email = None
            user.phone = None
            user.position = None
            user.password_hash = UNUSABLE_PASSWORD_HASH
            user.is_active = False
            user.invitation_pending = False
            user.must_change_password = False
            user.email_verified_at = None
            user.sessions_invalid_before = moment
        if user_ids:
            for model in (
                UserSession,
                MfaMethod,
                MfaRecoveryCode,
                PasswordResetToken,
                UserPermissionOverride,
                Notification,
                OrderCommentMention,
                OrderCommentRead,
            ):
                await db.execute(
                    delete(model)
                    .where(model.user_id.in_(user_ids))
                    .execution_options(synchronize_session=False)
                )
            await db.execute(
                delete(PortalAccountInvite)
                .where(PortalAccountInvite.user_id.in_(user_ids))
                .execution_options(synchronize_session=False)
            )

        portal = await _portal_translator(db, account)
        if portal is not None:
            order_ids = list(
                (
                    await db.execute(
                        select(PersonalOrder.id).where(
                            PersonalOrder.portal_translator_id == portal.id
                        )
                    )
                ).scalars()
            )
            for chunk in _chunks(order_ids):
                await db.execute(
                    delete(PersonalOrderFile)
                    .where(PersonalOrderFile.personal_order_id.in_(chunk))
                    .execution_options(synchronize_session=False)
                )
                await db.execute(
                    delete(PersonalOrderLanguagePair)
                    .where(PersonalOrderLanguagePair.personal_order_id.in_(chunk))
                    .execution_options(synchronize_session=False)
                )
            for statement in (
                delete(PersonalOrder).where(PersonalOrder.portal_translator_id == portal.id),
                delete(PortalAccountInvite).where(
                    PortalAccountInvite.portal_translator_id == portal.id
                ),
                delete(PortalTranslatorLink).where(
                    PortalTranslatorLink.portal_translator_id == portal.id
                ),
                delete(PortalTranslator).where(PortalTranslator.id == portal.id),
            ):
                await db.execute(statement.execution_options(synchronize_session=False))

        # What they typed to sign in is personal data in the attempts log too.
        typed = {value.lower() for value in (address, phone) if value}
        if typed:
            await db.execute(
                delete(LoginAttempt)
                .where(func.lower(LoginAttempt.username).in_(typed))
                .execution_options(synchronize_session=False)
            )
        await db.flush()

    account.email = None
    account.phone = None
    account.suliko_user_id = None
    account.full_name = DELETED_NAME
    account.password_hash = UNUSABLE_PASSWORD_HASH
    account.must_change_password = False
    account.email_verified_at = None
    request.status = DeletionStatus.DONE
    request.done_at = moment
    request.portal_deactivated = False
    await db.flush()
    return address, name


# ── The emails ─────────────────────────────────────────────────────────────
#
# Laid out like every platform mail (see core/mail_layout.py).


def _when(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else "just now"


def team_email(
    account: Account,
    request: AccountDeletionRequest,
    *,
    organisation: str,
    organisation_slug: str,
) -> Email:
    """The note to the team that a request has arrived."""
    name = account.full_name or login_name(account)
    return compose(
        subject=f"Account deletion request: {name}",
        heading="Account deletion request",
        preheader=f"{name} asked for their account to be deleted. Access is closed.",
        blocks=[
            p(
                Bold(name),
                " has asked for their Suliko Office account to be deleted. Their access is closed.",
            ),
            Facts(
                [
                    ("Name", name),
                    ("Email", account.email or "(none)"),
                    ("Phone", account.phone or "(none)"),
                    ("suliko.ge id", str(account.suliko_user_id or "(an Office-only account)")),
                    ("Asked from", f"{organisation} ({organisation_slug})"),
                    ("Request", f"{request.id}, filed {_when(request.created_at)}"),
                ]
            ),
            p(
                "Confirm it only if they have no obligations: unpaid invoices or orders "
                "in progress, payouts owed to a translator, an organisation they are the "
                "only owner of, or a suliko.ge balance owed. Once confirmed, the account "
                f"is deleted within {DELETION_GRACE_DAYS} days."
            ),
            Button("Review in the admin console", f"{get_settings().app_url.rstrip('/')}/admin"),
            callout(
                "If they write to cancel, reject it in the console, or run on the API server: ",
                Bold(f"python -m suliko.cli cancel-deletion-request --id {request.id}"),
            ),
        ],
        reason="You received this because you are on the Suliko team.",
    )


def person_email(account: Account) -> Email:
    """The confirmation to the person who asked."""
    support = get_settings().support_email
    return compose(
        subject="We have your request to delete your Suliko account",
        heading="We have your request",
        preheader="Your access is closed while we review it.",
        blocks=[
            p(f"Hello {account.full_name or login_name(account)},"),
            p(
                "We have your request to delete your Suliko Office account, and your "
                "access is closed from now on."
            ),
            p(
                "We look at every request. We confirm it only if you have no obligations, "
                "for example unpaid invoices, orders in progress, payouts still owed to a "
                "translator, or an organisation you are the only owner of. Once it is "
                f"confirmed, your account is deleted within {DELETION_GRACE_DAYS} days. "
                "Orders, clients and invoices stay with the organisation they belong to."
            ),
            callout(
                "Changed your mind? Write to ",
                Link(support, f"mailto:{support}"),
                " and we will cancel the request and open your account again.",
            ),
            fine("If you did not ask for this, write to us straight away."),
        ],
        reason="You received this because an account deletion was requested for this address.",
    )


def approved_email(account: Account, request: AccountDeletionRequest) -> Email:
    """Telling the person their request was confirmed."""
    support = get_settings().support_email
    name = account.full_name or login_name(account)
    return compose(
        subject="Your Suliko account will be deleted",
        heading="Your deletion is confirmed",
        preheader=f"Your account will be deleted on {_when(request.purge_after)}.",
        blocks=[
            p(f"Hello {name},"),
            p(
                "We have confirmed your request to delete your Suliko Office account. "
                "Your account and personal details will be deleted on ",
                Bold(_when(request.purge_after)),
                ". Orders, clients and invoices stay with the organisation they belong to.",
            ),
            callout(
                "Until then your account stays closed. If you change your mind, write to ",
                Link(support, f"mailto:{support}"),
                " before that date and we will cancel the deletion.",
            ),
        ],
        reason="You received this because you asked for your account to be deleted.",
    )


def rejected_email(account: Account) -> Email:
    """Telling the person their request ended and the account is open."""
    support = get_settings().support_email
    name = account.full_name or login_name(account)
    return compose(
        subject="Your Suliko account is open again",
        heading="Your account is open again",
        preheader="Nothing was deleted. You can sign in as before.",
        blocks=[
            p(f"Hello {name},"),
            p(
                "Your request to delete your Suliko Office account has been cancelled. "
                "Nothing was deleted, and you can sign in again as before."
            ),
            fine("If you have questions, write to ", Link(support, f"mailto:{support}"), "."),
        ],
        reason="You received this because you asked for your account to be deleted.",
    )


def done_email(name: str) -> Email:
    """Telling the person the deletion has happened."""
    return compose(
        subject="Your Suliko account has been deleted",
        heading="Your account has been deleted",
        preheader="Your name, contact details and sign-in are gone.",
        blocks=[
            p(f"Hello {name},"),
            p(
                "Your Suliko Office account has been deleted, as you asked. Your name, "
                "contact details and sign-in are gone. Orders, clients and invoices stay "
                "with the organisation they belong to."
            ),
            p("Thank you for using Suliko Office."),
        ],
        reason="You received this because you asked for your account to be deleted.",
    )


__all__ = [
    "DELETED_NAME",
    "DELETION_GRACE_DAYS",
    "AccountDeletionPendingError",
    "DeletionAlreadyRequestedError",
    "Finding",
    "ObligationsOpenError",
    "OpenRequest",
    "approve_request",
    "approved_email",
    "done_email",
    "due_for_purge",
    "ensure_not_pending_deletion",
    "file_request",
    "findings",
    "open_request",
    "open_requests",
    "organisations_left_without_an_owner",
    "person_email",
    "purge_account",
    "recent_decided_requests",
    "reject_request",
    "rejected_email",
    "team_email",
]
