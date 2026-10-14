"""Asking for an account to be deleted.

"Delete account" on the Account page is a request, not an act (decided
2026-10-14): the team reviews it and confirms it only if the person has no
obligations, and the account is deleted once that is confirmed. This module is
the first part, the request, and the rule that goes with it.

* `file_request` records it.
* While a request is open the account cannot sign in anywhere.
  `ensure_not_pending_deletion` is the one statement of that rule and every way of
  signing in calls it, so they cannot disagree. It is the ACCOUNT that is closed,
  not its memberships: they keep their own state, so ending the request leaves
  everything as it was.
* `reject_request` ends an open request the other way: the person wrote to cancel,
  or the review said no. It reopens the account.

Review, approval and the deletion itself come later; the statuses are here so
they have somewhere to land.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.config import get_settings
from suliko.core.errors import AppError, ConflictError, ValidationError
from suliko.db.tenancy import bypass_tenant_scope
from suliko.domain.accounts import login_name, would_strand_organisation
from suliko.models.tenant import Tenant
from suliko.models.user import (
    OPEN_DELETION_STATUSES,
    Account,
    AccountDeletionRequest,
    DeletionStatus,
    Role,
    User,
)

#: How long after the team confirms a request the account is deleted. Said to the
#: person in the confirmation; the deletion job that keeps the promise is separate.
DELETION_GRACE_DAYS = 30


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
    await db.flush()
    # `created_at` is the database's own clock; read it back rather than guess.
    await db.refresh(request)
    return request


async def reject_request(db: AsyncSession, request_id: int) -> AccountDeletionRequest:
    """End an open request without deleting anything: the account can sign in again."""
    request = await db.get(AccountDeletionRequest, request_id)
    if request is None:
        raise ValidationError(f"There is no deletion request {request_id}.")
    if request.status not in OPEN_DELETION_STATUSES:
        raise ValidationError(
            f"Request {request_id} is already {request.status.value}; there is nothing to cancel."
        )
    request.status = DeletionStatus.REJECTED
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


# ── The two emails ─────────────────────────────────────────────────────────
#
# Plain text, like every platform mail (see core/mail.py).


def _when(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else "just now"


def team_email(
    account: Account,
    request: AccountDeletionRequest,
    *,
    organisation: str,
    organisation_slug: str,
) -> tuple[str, str]:
    """Subject and body of the note to the team that a request has arrived."""
    name = account.full_name or login_name(account)
    body = (
        f"{name} has asked for their Suliko Office account to be deleted. "
        "Their access is closed.\n\n"
        f"  Name:           {name}\n"
        f"  Email:          {account.email or '(none)'}\n"
        f"  Phone:          {account.phone or '(none)'}\n"
        f"  suliko.ge id:   {account.suliko_user_id or '(an Office-only account)'}\n"
        f"  Asked from:     {organisation} ({organisation_slug})\n"
        f"  Request:        {request.id}, filed {_when(request.created_at)}\n\n"
        "Confirm it only if they have no obligations: unpaid invoices or orders "
        "in progress, payouts owed to a translator, an organisation they are the "
        "only owner of, or a suliko.ge balance owed. Once confirmed, the account "
        f"is deleted within {DELETION_GRACE_DAYS} days.\n\n"
        "If they write to cancel, run on the API server:\n\n"
        f"  python -m suliko.cli cancel-deletion-request --id {request.id}\n"
    )
    return f"Account deletion request: {name}", body


def person_email(account: Account) -> tuple[str, str]:
    """Subject and body of the confirmation to the person who asked."""
    support = get_settings().support_email
    body = (
        f"Hello {account.full_name or login_name(account)},\n\n"
        "We have your request to delete your Suliko Office account, and your "
        "access is closed from now on.\n\n"
        "We look at every request. We confirm it only if you have no obligations, "
        "for example unpaid invoices, orders in progress, payouts still owed to a "
        "translator, or an organisation you are the only owner of. Once it is "
        f"confirmed, your account is deleted within {DELETION_GRACE_DAYS} days. "
        "Orders, clients and invoices stay with the organisation they belong to.\n\n"
        f"Changed your mind? Write to {support} and we will cancel the request and "
        "open your account again.\n\n"
        "If you did not ask for this, write to us straight away.\n"
    )
    return "We have your request to delete your Suliko account", body


__all__ = [
    "DELETION_GRACE_DAYS",
    "AccountDeletionPendingError",
    "DeletionAlreadyRequestedError",
    "OpenRequest",
    "ensure_not_pending_deletion",
    "file_request",
    "open_request",
    "open_requests",
    "organisations_left_without_an_owner",
    "person_email",
    "reject_request",
    "team_email",
]
