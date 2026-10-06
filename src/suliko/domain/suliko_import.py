"""Bringing suliko.ge's people into Suliko Office, and fixing up the odd one by hand.

`import_users` walks suliko.ge's whole directory and gives each person an
account here through the one rule in `domain.accounts.upsert_from_suliko`. It
is safe to run again: a person already linked is left alone, so a second run
only picks up whoever registered since. People who register later are also
picked up on their own the first time they sign in, so nobody depends on this
being re-run.

Run with `dry_run=True` it writes nothing and reports exactly what a real run
would do — in particular WHICH EXISTING Office accounts would start answering
to a suliko.ge password instead of their own, which is the one effect worth
reading before it happens.

The caller owns the transaction: nothing here commits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.core.errors import ValidationError
from suliko.db.tenancy import bypass_tenant_scope
from suliko.domain.accounts import (
    SulikoAccountConflictError,
    find_account,
    find_account_by_login,
    find_account_by_suliko_id,
    link_account_to_suliko,
    login_name,
    upsert_from_suliko,
)
from suliko.integrations.suliko_backend import SulikoBackend
from suliko.models.user import Account, User


@dataclass
class ImportReport:
    #: People suliko.ge listed.
    seen: int = 0
    #: Already linked: left alone.
    already_linked: int = 0
    #: New accounts (made, or — in a dry run — that would be made).
    created: int = 0
    #: Sign-ins of Office accounts that existed before and now use the
    #: suliko.ge password. Their own password stops working.
    linked_existing: list[str] = field(default_factory=list)
    #: Sign-ins that two different people claim. Left untouched.
    conflicts: list[str] = field(default_factory=list)
    #: Office accounts still with no suliko.ge person behind them afterwards.
    #: They keep a password of their own — platform operators, and anyone who
    #: registered on suliko.ge with a different sign-in (a phone number, say).
    office_only: list[str] = field(default_factory=list)


async def import_users(db: AsyncSession, backend: SulikoBackend, *, dry_run: bool) -> ImportReport:
    report = ImportReport()
    async for person in backend.iter_users():
        report.seen += 1

        if await find_account_by_suliko_id(db, person.id) is not None:
            report.already_linked += 1
            continue

        if dry_run:
            office = await find_account_by_login(db, person.user_name)
            if office is None:
                report.created += 1
            elif office.suliko_user_id is None:
                report.linked_existing.append(person.user_name)
            else:
                report.conflicts.append(person.user_name)
            continue

        try:
            link = await upsert_from_suliko(db, person)
        except SulikoAccountConflictError:
            report.conflicts.append(person.user_name)
            continue
        if link.created:
            report.created += 1
        elif link.linked_existing:
            report.linked_existing.append(person.user_name)

    will_link = {login.lower() for login in report.linked_existing}
    unlinked = (
        (await db.execute(select(Account).where(Account.suliko_user_id.is_(None)))).scalars().all()
    )
    report.office_only = sorted(
        login_name(account) for account in unlinked if login_name(account).lower() not in will_link
    )
    return report


async def link_by_hand(
    db: AsyncSession, backend: SulikoBackend, *, office_email: str, suliko_login: str
) -> str:
    """Link an existing Office account to the suliko.ge person who signs in with
    `suliko_login` — for someone whose suliko.ge sign-in is a phone number, so
    no address ever matched. Returns what was done, for the operator to read.

    If a run of the import already made an empty account for that person, it is
    removed first; one that already has organisations is never touched.
    """
    office = await find_account(db, office_email)
    if office is None:
        raise ValidationError(f"No Office account uses {office_email}.")
    if office.suliko_user_id is not None:
        raise ValidationError(f"{office_email} is already linked to a suliko.ge person.")

    person = await backend.find_user(suliko_login)
    if person is None:
        raise ValidationError(f"suliko.ge has nobody who signs in with {suliko_login}.")

    duplicate = await find_account_by_suliko_id(db, person.id)
    removed = ""
    if duplicate is not None and duplicate.id != office.id:
        with bypass_tenant_scope():
            memberships = await db.scalar(
                select(func.count()).select_from(User).where(User.account_id == duplicate.id)
            )
        if memberships:
            raise ValidationError(
                f"{login_name(duplicate)} already has {memberships} organisation(s) in Office; "
                "merge those by hand first."
            )
        await db.delete(duplicate)
        await db.flush()
        removed = f" Removed the empty account the import had made for {login_name(duplicate)}."

    link_account_to_suliko(office, person, now=datetime.now(UTC))
    await db.flush()
    return (
        f"{office_email} now signs in with suliko.ge ({person.user_name}).{removed} "
        "Its Office password no longer works."
    )
