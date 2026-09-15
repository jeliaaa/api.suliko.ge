"""Portal lookups: who a suliko.ge user is here, and what they may see.

Functions taking ``platform_db`` read platform tables only and must be given a
session with no tenant bound. Functions taking ``tenant_db`` read one bureau's
data and must be given a session opened inside that bureau's scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.models.directory import Client, Translator
from suliko.models.order import Order, OrderDocument
from suliko.models.portal import PortalTranslator, PortalTranslatorLink
from suliko.models.reference import DocumentType
from suliko.models.tenant import Tenant, TenantStatus

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


def match_reason(row: Translator, *, phone: str | None, email: str | None) -> MatchReason | None:
    """How a directory row matches an account: 'phone', 'email', both, or None."""
    wanted_phone, wanted_email = normalize_phone(phone), normalize_email(email)
    by_phone = wanted_phone is not None and normalize_phone(row.phone) == wanted_phone
    by_email = wanted_email is not None and normalize_email(row.email) == wanted_email
    if by_phone and by_email:
        return "phone_and_email"
    if by_phone:
        return "phone"
    if by_email:
        return "email"
    return None


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
