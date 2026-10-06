"""People, their accounts, and the organisations they belong to.

An `Account` is a person: one sign-in (an email, or a phone number for
someone who registered on suliko.ge with one) and one password. Each
organisation they belong to is a `users` row whose `account_id` points at it,
carrying that organisation's role and permissions. This module is the one
place that answers "which organisations can this person enter?" and that
creates the personal workspace, so the sign-in chooser, the in-app switcher
and invitations cannot disagree about either.

It is also where a person on suliko.ge becomes an account here
(`upsert_from_suliko`): sign-in, the invite search and the import all go
through that one rule, so they cannot disagree about who is who.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.config import get_settings
from suliko.core.errors import ConflictError, ValidationError
from suliko.db.session import bind_tenant_guc
from suliko.db.tenancy import bypass_tenant_scope, tenant_scope
from suliko.domain.plans import TenantPlan
from suliko.domain.reference_seed import seed_reference_data
from suliko.integrations.suliko_backend import SulikoUser
from suliko.models.reference import TenantSettings
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Account, Role, User

#: What a `users` row now holds in `password_hash`. The column predates
#: accounts and is NOT NULL; the password that signs a person in is the
#: account's, so a membership's own is deliberately unverifiable.
UNUSABLE_PASSWORD_HASH = "!"  # noqa: S105 -- deliberately matches no password


def normalise_email(email: str) -> str:
    return email.strip().lower()


def username_for(email: str) -> str:
    """`users.username` is String(100); the address is the username."""
    email = normalise_email(email)
    return email if len(email) <= 100 else email.split("@")[0][:100]


def login_name(account: Account) -> str:
    """What this person signs in with: their address, else their phone."""
    return account.email or account.phone or ""


def username_for_account(account: Account) -> str:
    """`users.username` for a membership of this account."""
    if account.email:
        return username_for(account.email)
    return (account.phone or "").strip()[:100]


async def find_account(db: AsyncSession, email: str) -> Account | None:
    return (
        await db.execute(select(Account).where(Account.email == normalise_email(email)))
    ).scalar_one_or_none()


async def find_account_by_login(db: AsyncSession, login: str) -> Account | None:
    """The account an email or a phone number belongs to."""
    login = login.strip()
    if "@" in login:
        return await find_account(db, login)
    return (await db.execute(select(Account).where(Account.phone == login))).scalar_one_or_none()


async def find_account_by_suliko_id(db: AsyncSession, suliko_user_id: str) -> Account | None:
    return (
        await db.execute(select(Account).where(Account.suliko_user_id == suliko_user_id))
    ).scalar_one_or_none()


class SulikoAccountConflictError(ConflictError):
    """Two different people claim one sign-in. Needs a person to look at it."""

    error_code = "suliko_account_conflict"


@dataclass(frozen=True, slots=True)
class SulikoLink:
    account: Account
    #: A new account was made for them.
    created: bool
    #: An Office account that already existed (same address) now answers to
    #: suliko.ge's password instead of its own.
    linked_existing: bool


def link_account_to_suliko(account: Account, person: SulikoUser, *, now: datetime) -> None:
    """Make `account` this suliko.ge person's, whose password is the one on suliko.ge.

    The account's own password stops working: it is replaced by one that
    verifies as nothing, so unlinking later cannot revive a stale password.
    """
    account.suliko_user_id = person.id
    if person.phone and not account.phone:
        account.phone = person.phone
    account.password_hash = UNUSABLE_PASSWORD_HASH
    account.must_change_password = False
    if not account.full_name.strip():
        account.full_name = person.full_name or person.user_name
    # suliko.ge proved this address at registration (a code sent to it, or the
    # provider's own verification), so it is as good as confirmed here.
    if person.email and account.email == person.email and account.email_verified_at is None:
        account.email_verified_at = now


async def upsert_from_suliko(db: AsyncSession, person: SulikoUser) -> SulikoLink:
    """The one rule for turning a person on suliko.ge into an account here.

    1. Already linked by suliko.ge id: that account.
    2. Their sign-in address belongs to an Office account that is not linked
       to anyone: link it. Same address, proven on suliko.ge.
    3. Otherwise a new account, with the address or the phone they sign in
       with and no password of its own (it is suliko.ge's).

    Identity is the suliko.ge id; the address or phone is only what they
    type. A different suliko.ge person holding an address that an Office
    account has linked elsewhere is a conflict, never a quiet takeover.
    """
    now = datetime.now(UTC)

    linked = await find_account_by_suliko_id(db, person.id)
    if linked is not None:
        return SulikoLink(linked, created=False, linked_existing=False)

    office = await find_account_by_login(db, person.user_name)
    if office is not None:
        if office.suliko_user_id not in (None, person.id):
            raise SulikoAccountConflictError(
                "This sign-in is already linked to another person. Contact support."
            )
        link_account_to_suliko(office, person, now=now)
        await db.flush()
        return SulikoLink(office, created=False, linked_existing=True)

    account = Account(
        email=person.email,
        phone=person.phone,
        suliko_user_id=person.id,
        password_hash=UNUSABLE_PASSWORD_HASH,
        full_name=person.full_name or person.user_name,
        must_change_password=False,
        email_verified_at=now if person.email else None,
    )
    db.add(account)
    await db.flush()
    return SulikoLink(account, created=True, linked_existing=False)


@dataclass(frozen=True, slots=True)
class Membership:
    user: User
    tenant: Tenant


async def memberships(db: AsyncSession, account_id: int) -> list[Membership]:
    """Every organisation this person can enter right now, one entry each.

    Active, accepted memberships of usable organisations only. Should an
    organisation hold two rows for the same person (a case-only duplicate from
    imported data), the one used most recently represents it.
    """
    with bypass_tenant_scope():
        rows = (
            await db.execute(
                select(User, Tenant)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(
                    User.account_id == account_id,
                    User.is_active.is_(True),
                    User.invitation_pending.is_(False),
                )
                .order_by(Tenant.display_name, User.last_login_at.desc().nulls_last(), User.id)
            )
        ).all()
    seen: set[int] = set()
    out: list[Membership] = []
    for user, tenant in rows:
        if not tenant.is_usable or tenant.id in seen:
            continue
        seen.add(tenant.id)
        out.append(Membership(user=user, tenant=tenant))
    return out


def personal(options: list[Membership]) -> Membership | None:
    """The person's own freelancer workspace among their memberships, if any."""
    return next(
        (m for m in options if m.tenant.is_personal and m.user.role is Role.OWNER),
        None,
    )


def slug_candidate(name: str) -> str:
    """A URL-safe slug from an organisation name.

    Georgian is transliterated to nothing useful by any cheap scheme, so a
    name with no ASCII letters falls back to a generic stem plus the
    uniqueness suffix — `bureau-4` is a worse handle than
    `tbilisi-translations`, but one that can be read back over the phone.
    """
    ascii_only = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:40].strip("-")
    # The slug column demands 2-63 chars starting alphanumeric.
    return slug if len(slug) >= 2 else "bureau"


async def unique_slug(db: AsyncSession, name: str) -> str:
    """The candidate, or the first free `-N` suffix after it."""
    base = slug_candidate(name)
    with bypass_tenant_scope():
        taken = set(
            (
                await db.execute(
                    select(Tenant.slug).where(
                        or_(Tenant.slug == base, Tenant.slug.like(f"{base}-%"))
                    )
                )
            )
            .scalars()
            .all()
        )
    if base not in taken:
        return base
    # Bounded: a name colliding 999 times is abuse, not a naming coincidence.
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"
        if candidate not in taken:
            return candidate
    raise ConflictError("Could not allocate an organisation handle. Try a different name.")


async def _found_workspace(
    db: AsyncSession,
    account: Account,
    *,
    name: str,
    plan: TenantPlan | None,
    is_personal: bool,
) -> Membership:
    """A new organisation owned by `account` — the one way one is made, for
    sign-up, the chooser, the switcher and the personal account alike.

    The starter catalogues are the same ones `suliko seed-reference` adds,
    minus prices (see domain/reference_seed.py): without them the very first
    order has no document type to choose.
    """
    settings = get_settings()
    slug = await unique_slug(db, name)

    with bypass_tenant_scope():
        tenant = Tenant(
            slug=slug,
            display_name=name,
            status=TenantStatus.TRIAL,
            plan=plan.value if plan else None,
            locale=settings.default_signup_locale,
            is_personal=is_personal,
        )
        db.add(tenant)
        await db.flush()

    tenant_id = int(tenant.id)
    # This session may have been opened before the tenant existed, so nothing
    # has set the RLS GUC for the rows below.
    await bind_tenant_guc(db, tenant_id)
    with tenant_scope(tenant_id):
        db.add(TenantSettings(tenant_id=tenant_id, default_language=tenant.locale))
        await seed_reference_data(db)
        user = User(
            tenant_id=tenant_id,
            account_id=account.id,
            username=username_for_account(account),
            email=account.email,
            full_name=account.full_name.strip() or name,
            password_hash=UNUSABLE_PASSWORD_HASH,
            role=Role.OWNER,
            is_active=True,
            email_verified_at=account.email_verified_at,
        )
        db.add(user)
        await db.flush()
    return Membership(user=user, tenant=tenant)


async def create_personal_workspace(db: AsyncSession, account: Account) -> Membership:
    """The person's own organisation, on the Freelancer plan, owned by them.

    Created the first time they pick "Personal account".
    """
    name = account.full_name.strip() or login_name(account).split("@")[0]
    return await _found_workspace(
        db, account, name=name, plan=TenantPlan.FREELANCER, is_personal=True
    )


async def create_bureau(
    db: AsyncSession,
    account: Account,
    name: str,
    *,
    plan: TenantPlan | None = TenantPlan.BUREAU,
) -> Membership:
    """A new bureau owned by this account.

    From the chooser and the switcher it is a bureau from the start and stays
    one (see `api/v1/tenant.py`): freelance work belongs in the personal
    account. Sign-up passes `plan=None`, "has not chosen yet", and its owner
    picks on the onboarding screen.
    """
    name = " ".join(name.split())
    if len(name) < 2:
        raise ValidationError("Enter the bureau's name.")
    return await _found_workspace(db, account, name=name, plan=plan, is_personal=False)


async def revoke_account_sessions(db: AsyncSession, account_id: int) -> None:
    """Sign the person out everywhere — every organisation, every device.

    A password belongs to the account, so a change or reset has to reach the
    sessions of every membership, not just the one it was made from.
    """
    with bypass_tenant_scope():
        await db.execute(
            update(User)
            .where(User.account_id == account_id)
            .values(sessions_invalid_before=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )


__all__ = [
    "UNUSABLE_PASSWORD_HASH",
    "Membership",
    "SulikoAccountConflictError",
    "SulikoLink",
    "create_bureau",
    "create_personal_workspace",
    "find_account",
    "find_account_by_login",
    "find_account_by_suliko_id",
    "link_account_to_suliko",
    "login_name",
    "memberships",
    "normalise_email",
    "personal",
    "revoke_account_sessions",
    "slug_candidate",
    "unique_slug",
    "upsert_from_suliko",
    "username_for",
    "username_for_account",
]
