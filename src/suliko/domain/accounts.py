"""People, their accounts, and the organisations they belong to.

An `Account` is a person: one email, one password. Each organisation they
belong to is a `users` row whose `account_id` points at it, carrying that
organisation's role and permissions. This module is the one place that
answers "which organisations can this person enter?" and that creates the
personal workspace, so the sign-in chooser, the in-app switcher and
invitations cannot disagree about either.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.config import get_settings
from suliko.core.errors import ConflictError
from suliko.db.session import bind_tenant_guc
from suliko.db.tenancy import bypass_tenant_scope, tenant_scope
from suliko.domain.plans import TenantPlan
from suliko.domain.reference_seed import seed_reference_data
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


async def find_account(db: AsyncSession, email: str) -> Account | None:
    return (
        await db.execute(select(Account).where(Account.email == normalise_email(email)))
    ).scalar_one_or_none()


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


async def create_personal_workspace(db: AsyncSession, account: Account) -> Membership:
    """The person's own organisation, on the Freelancer plan, owned by them.

    Created the first time they pick "Personal account". Seeded with the
    same starter catalogues as a sign-up, so the first order has document
    types to choose from.
    """
    settings = get_settings()
    name = account.full_name.strip() or account.email.split("@")[0]
    slug = await unique_slug(db, name)

    with bypass_tenant_scope():
        tenant = Tenant(
            slug=slug,
            display_name=name,
            status=TenantStatus.TRIAL,
            plan=TenantPlan.FREELANCER.value,
            locale=settings.default_signup_locale,
            is_personal=True,
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
            username=username_for(account.email),
            email=account.email,
            full_name=name,
            password_hash=UNUSABLE_PASSWORD_HASH,
            role=Role.OWNER,
            is_active=True,
            email_verified_at=account.email_verified_at,
        )
        db.add(user)
        await db.flush()
    return Membership(user=user, tenant=tenant)


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
    "create_personal_workspace",
    "find_account",
    "memberships",
    "normalise_email",
    "personal",
    "revoke_account_sessions",
    "slug_candidate",
    "unique_slug",
    "username_for",
]
