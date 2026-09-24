"""Portal lookups: who a suliko.ge user is here, and what they may see.

Functions taking ``platform_db`` read platform tables only and must be given a
session with no tenant bound. Functions taking ``tenant_db`` read one bureau's
data and must be given a session opened inside that bureau's scope.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.core.errors import ConflictError, NotFoundError
from suliko.models.audit import ActorType
from suliko.models.directory import Client, Translator
from suliko.models.order import Order, OrderDocument
from suliko.models.portal import (
    InviteKind,
    InviteStatus,
    PortalAccountInvite,
    PortalTranslator,
    PortalTranslatorLink,
)
from suliko.models.reference import DocumentType
from suliko.models.tenant import Tenant, TenantStatus

#: A tenant session factory, as `api/portal_deps.py` defines and constructs it.
#: Duplicated here rather than imported so the domain layer does not depend on
#: the api layer — nothing else in this module does either.
TenantSessionFactory = Callable[[int], AbstractAsyncContextManager[AsyncSession]]

#: Newest first, per bureau. The portal is a work list, not an archive.
ASSIGNMENT_LIMIT = 200

USABLE_TENANT_STATUSES = (TenantStatus.ACTIVE, TenantStatus.TRIAL)

MatchReason = Literal["phone", "email", "phone_and_email"]


@dataclass(frozen=True, slots=True)
class LinkedOrganization:
    link_id: int
    tenant_id: int
    slug: str
    name: str
    #: This translator's row in the bureau's own ``translators`` directory.
    translator_id: int


@dataclass(frozen=True, slots=True)
class AssignedDocument:
    document: OrderDocument
    document_type_name: str | None


@dataclass(slots=True)
class AssignedOrder:
    order: Order
    client_name: str
    documents: list[AssignedDocument] = field(default_factory=list)

    @property
    def order_date(self) -> date:
        return self.order.order_date


# ── Platform lookups ────────────────────────────────────────────────────────


async def find_portal_translator(
    platform_db: AsyncSession, external_user_id: str
) -> PortalTranslator | None:
    return (
        await platform_db.execute(
            select(PortalTranslator).where(PortalTranslator.external_user_id == external_user_id)
        )
    ).scalar_one_or_none()


async def linked_organizations(
    platform_db: AsyncSession, portal_translator_id: int, *, include_suspended: bool = False
) -> list[LinkedOrganization]:
    """The bureaus a translator works for.

    Suspended bureaus are hidden from the portal: their data is frozen, and a
    translator should not keep working on it. The admin still sees the link.
    """
    stmt = (
        select(PortalTranslatorLink, Tenant)
        .join(Tenant, Tenant.id == PortalTranslatorLink.tenant_id)
        .where(PortalTranslatorLink.portal_translator_id == portal_translator_id)
        .order_by(Tenant.display_name)
    )
    if not include_suspended:
        stmt = stmt.where(Tenant.status.in_(USABLE_TENANT_STATUSES))

    return [
        LinkedOrganization(
            link_id=link.id,
            tenant_id=tenant.id,
            slug=tenant.slug,
            name=tenant.display_name,
            translator_id=link.translator_id,
        )
        for link, tenant in (await platform_db.execute(stmt)).all()
    ]


async def find_tenant_by_slug(platform_db: AsyncSession, slug: str) -> Tenant | None:
    return (
        await platform_db.execute(select(Tenant).where(Tenant.slug == slug))
    ).scalar_one_or_none()


# ── Matching a suliko.ge account to a bureau's directory row ────────────────


def normalize_phone(phone: str | None) -> str | None:
    """Digits only, without Georgia's country code.

    So "+995 555 12-34-56", "995555123456" and "555123456" all compare equal. Fewer
    than six digits is treated as no number, so junk like "-" matches nothing.
    """
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    if digits.startswith("995") and len(digits) == 12:
        digits = digits[3:]
    return digits if len(digits) >= 6 else None


def normalize_email(email: str | None) -> str | None:
    value = (email or "").strip().lower()
    return value or None


def contact_match(
    row_phone: str | None, row_email: str | None, *, phone: str | None, email: str | None
) -> MatchReason | None:
    """Whether a (phone, email) pair matches a wanted phone/email, and how.

    The one definition of "same person" in this module, both directions share
    it: `match_reason` compares a bureau's directory row against a suliko.ge
    account, `account_match_reason` compares a suliko.ge account against a
    bureau's invite. Neither side is privileged — both are normalized before
    comparing.
    """
    wanted_phone, wanted_email = normalize_phone(phone), normalize_email(email)
    by_phone = wanted_phone is not None and normalize_phone(row_phone) == wanted_phone
    by_email = wanted_email is not None and normalize_email(row_email) == wanted_email
    if by_phone and by_email:
        return "phone_and_email"
    if by_phone:
        return "phone"
    if by_email:
        return "email"
    return None


def match_reason(row: Translator, *, phone: str | None, email: str | None) -> MatchReason | None:
    """How a directory row matches an account: 'phone', 'email', both, or None."""
    return contact_match(row.phone, row.email, phone=phone, email=email)


def account_match_reason(
    row: PortalTranslator, *, phone: str | None, email: str | None
) -> MatchReason | None:
    """How a suliko.ge account matches a wanted phone/email, and how."""
    return contact_match(row.phone, row.email, phone=phone, email=email)


async def directory_matches(
    tenant_db: AsyncSession, *, phone: str | None, email: str | None
) -> list[tuple[Translator, MatchReason]]:
    """The bureau's translators that share the account's phone or email.

    Filtered in Python rather than SQL because phone numbers are stored however
    staff typed them; a directory is hundreds of rows, not millions.
    """
    rows = (await tenant_db.execute(select(Translator).order_by(Translator.name))).scalars().all()
    matches: list[tuple[Translator, MatchReason]] = []
    for row in rows:
        reason = match_reason(row, phone=phone, email=email)
        if reason:
            matches.append((row, reason))
    return matches


async def account_matches(
    platform_db: AsyncSession, *, phone: str | None, email: str | None
) -> list[tuple[PortalTranslator, MatchReason]]:
    """The suliko.ge accounts that share a wanted phone or email.

    The mirror image of `directory_matches`: there it is a bureau's directory
    scanned for a suliko.ge account's contact details; here it is every
    suliko.ge account marked as a translator, scanned for a bureau's invite.
    Same reason for filtering in Python rather than SQL — a platform-wide
    table of translator accounts, not millions of rows, and contact details
    are stored however the suliko.ge admin typed them.

    Only accounts a suliko.ge admin has already marked as a translator are
    visible here — see the module docstring on `PortalAccountInvite` for what
    happens when a match appears later.
    """
    rows = (
        (
            await platform_db.execute(
                select(PortalTranslator).order_by(PortalTranslator.display_name)
            )
        )
        .scalars()
        .all()
    )
    matches: list[tuple[PortalTranslator, MatchReason]] = []
    for row in rows:
        reason = account_match_reason(row, phone=phone, email=email)
        if reason:
            matches.append((row, reason))
    return matches


async def search_directory(
    tenant_db: AsyncSession, search: str, limit: int = 20
) -> list[Translator]:
    pattern = f"%{search}%"
    return list(
        (
            await tenant_db.execute(
                select(Translator)
                .where(
                    or_(
                        Translator.name.ilike(pattern),
                        Translator.email.ilike(pattern),
                        Translator.phone.ilike(pattern),
                    )
                )
                .order_by(Translator.name)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


#: Where suliko.ge itself signs someone up. A bureau's invite that matches no
#: account links here, next to the address it must be registered with — see
#: `registration_url` and the two invite handlers in `api/v1/translators.py`
#: and `api/v1/users.py`.
REGISTRATION_PATH = "/register"


def registration_url() -> str:
    from suliko.config import get_settings

    return f"{get_settings().suliko_site_url.rstrip('/')}{REGISTRATION_PATH}"


# ── Linking an account to a directory row ───────────────────────────────────


@dataclass(frozen=True, slots=True)
class LinkResult:
    translator_id: int
    #: The row this account was linked to before, if this call replaced it.
    previous_translator_id: int | None
    created_directory_entry: bool


async def link_account_to_directory_row(
    platform_db: AsyncSession,
    tenants: TenantSessionFactory,
    *,
    account: PortalTranslator,
    tenant: Tenant,
    translator_id: int | None,
) -> LinkResult:
    """Link a suliko.ge account to one of a bureau's directory rows.

    The one implementation of the rule that matters here: an account has at
    most one row per bureau, and a row already linked to a DIFFERENT account is
    refused rather than silently reassigned (`uq_portal_link_directory_row`
    enforces the same thing at the database level). Shared by the suliko.ge
    admin's manual link (`portal_admin.link_organization`) and invite
    resolution (`resolve_pending_invites`), so the guard has one place to be
    right.

    ``translator_id=None`` creates a new `Translator` row seeded from the
    account's own details — the admin's "create one" option. Re-linking to a
    different row replaces the old link.

    Raises `NotFoundError` if `translator_id` names a row from another bureau
    (indistinguishable from a missing one, on purpose — see
    `portal_admin._tenant`), and `ConflictError` if that row is already linked
    to a different account.
    """
    if translator_id is not None:
        taken = (
            await platform_db.execute(
                select(PortalTranslatorLink).where(
                    PortalTranslatorLink.tenant_id == tenant.id,
                    PortalTranslatorLink.translator_id == translator_id,
                )
            )
        ).scalar_one_or_none()
        if taken is not None and taken.portal_translator_id != account.id:
            raise ConflictError(
                "That directory entry is already linked to another suliko.ge account."
            )

    created = False
    async with tenants(tenant.id) as tenant_db:
        if translator_id is not None:
            directory_row = await tenant_db.get(Translator, translator_id)
            if directory_row is None:
                raise NotFoundError("That translator is not in this organisation's directory.")
        else:
            directory_row = Translator(
                name=account.display_name,
                phone=account.phone,
                email=account.email,
                is_active=True,
                comment="Added from the suliko.ge admin panel for a translator portal account.",
            )
            tenant_db.add(directory_row)
            await tenant_db.flush()
            created = True
        directory_row_id = directory_row.id

    link = (
        await platform_db.execute(
            select(PortalTranslatorLink).where(
                PortalTranslatorLink.portal_translator_id == account.id,
                PortalTranslatorLink.tenant_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    previous = link.translator_id if link else None
    if link is None:
        platform_db.add(
            PortalTranslatorLink(
                portal_translator_id=account.id,
                tenant_id=tenant.id,
                translator_id=directory_row_id,
            )
        )
    else:
        link.translator_id = directory_row_id
    await platform_db.flush()

    return LinkResult(
        translator_id=directory_row_id,
        previous_translator_id=previous,
        created_directory_entry=created,
    )


# ── Resolving a bureau's invite against a suliko.ge account ─────────────────


async def resolve_pending_invites(
    platform_db: AsyncSession,
    tenants: TenantSessionFactory,
    account: PortalTranslator,
    *,
    actor_type: ActorType,
) -> list[PortalAccountInvite]:
    """Link every pending invite that now matches this suliko.ge account.

    Called from the two moments a match can newly come to exist:

    - a suliko.ge admin marks an account a translator
      (`portal_admin.upsert_translator`) — every bureau that invited this
      address before the account existed is served at once;
    - the translator opens their own portal (`portal.get_me`) — catches an
      invite written AFTER the account already existed, which the admin path
      above never sees.

    Both read `normalized_email` / `normalized_phone`, which are indexed,
    rather than scanning in Python like `account_matches` — that one runs once
    per invite at invite time; this one runs on every portal sign-in.

    A `kind='translator'` invite already names the directory row it targets
    (set when the invite was created — see `api/v1/translators.py`), so
    resolving it re-attaches that SAME row through
    `link_account_to_directory_row` rather than creating a new one. If the row
    was claimed by a different account in the meantime, this invite is left
    pending rather than losing it — one invite's conflict must not stop the
    others in the batch from resolving. A `kind='staff'` invite has no
    directory row to link; resolving it only records which account the
    address belongs to.
    """
    normalized_phone = normalize_phone(account.phone)
    normalized_email = normalize_email(account.email)
    if normalized_phone is None and normalized_email is None:
        return []

    conditions = []
    if normalized_email is not None:
        conditions.append(PortalAccountInvite.normalized_email == normalized_email)
    if normalized_phone is not None:
        conditions.append(PortalAccountInvite.normalized_phone == normalized_phone)

    rows = (
        (
            await platform_db.execute(
                select(PortalAccountInvite).where(
                    PortalAccountInvite.status == InviteStatus.PENDING, or_(*conditions)
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    from suliko.core.audit import record

    resolved: list[PortalAccountInvite] = []
    for invite in rows:
        tenant = await platform_db.get(Tenant, invite.tenant_id)
        if tenant is None:
            continue

        if invite.kind is InviteKind.TRANSLATOR:
            try:
                result = await link_account_to_directory_row(
                    platform_db,
                    tenants,
                    account=account,
                    tenant=tenant,
                    translator_id=invite.translator_id,
                )
            except (ConflictError, NotFoundError):
                continue
            invite.translator_id = result.translator_id

        invite.portal_translator_id = account.id
        invite.status = InviteStatus.LINKED
        invite.resolved_at = datetime.now(UTC)
        resolved.append(invite)

        await record(
            platform_db,
            None,
            action="portal.translator_claimed",
            entity_type="portal_account_invite",
            entity_id=invite.id,
            tenant_id=invite.tenant_id,
            actor_type=actor_type,
            after={
                "portal_translator": account.external_user_id,
                "email": invite.email,
                "kind": invite.kind.value,
            },
        )

    await platform_db.flush()
    return resolved


# ── Assignments inside one bureau ───────────────────────────────────────────


async def assigned_orders(
    tenant_db: AsyncSession,
    translator_id: int,
    *,
    order_id: int | None = None,
    limit: int = ASSIGNMENT_LIMIT,
) -> list[AssignedOrder]:
    """Orders with at least one document assigned to this directory row.

    Only the assigned documents are returned: a translator must not learn what
    else is in the order, who else is on it, or what anything costs.

    The id query is keyed on ``translator_id``, a row of THIS bureau's directory
    taken from the caller's link, so it cannot reach another bureau's orders
    even where the ORM filter does not rewrite it; RLS covers it regardless.
    """
    order_ids_stmt = (
        select(Order.id)
        .join(OrderDocument, OrderDocument.order_id == Order.id)
        .where(OrderDocument.translator_id == translator_id)
        .group_by(Order.id)
        .order_by(Order.id.desc())
        .limit(limit)
    )
    if order_id is not None:
        order_ids_stmt = order_ids_stmt.where(Order.id == order_id)
    order_ids = list((await tenant_db.execute(order_ids_stmt)).scalars().all())
    if not order_ids:
        return []

    rows = (
        await tenant_db.execute(
            select(Order, Client.name, OrderDocument, DocumentType.name_en)
            .join(Client, Client.id == Order.client_id)
            .join(OrderDocument, OrderDocument.order_id == Order.id)
            .outerjoin(DocumentType, DocumentType.id == OrderDocument.document_type_id)
            .where(Order.id.in_(order_ids), OrderDocument.translator_id == translator_id)
            .order_by(Order.id.desc(), OrderDocument.id)
        )
    ).all()

    grouped: dict[int, AssignedOrder] = {}
    for order, client_name, document, type_name in rows:
        entry = grouped.setdefault(order.id, AssignedOrder(order=order, client_name=client_name))
        entry.documents.append(AssignedDocument(document=document, document_type_name=type_name))
    return list(grouped.values())
