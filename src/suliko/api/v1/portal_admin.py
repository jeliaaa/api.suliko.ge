"""suliko.ge admin: who is a translator, and which bureaus they work for.

The suliko.ge admin panel manages this rather than a bureau's CRM admins,
because a translator can work for several bureaus and no single bureau owns the
relationship. The admin is recognised by the ``adm`` flag in the signed
assertion, which the suliko.ge server sets only for the user ids on its own
admin allowlist.

What an admin can do here is deliberately narrow:

- mark a suliko.ge account as a translator, or deactivate it;
- link it to a bureau's directory row — an existing one, or a new one;
- record which Shared Drive a bureau's order files go to.

An admin cannot read a bureau's orders, clients or money from here. Linking a
translator exposes only the documents the bureau itself assigns to that
directory row. Every change is written to the audit log.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Path, Query
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.portal_deps import Drive, PlatformDb, PortalAdmin, PortalIdentity, TenantSessions
from suliko.core.errors import (
    ConflictError,
    NotFoundError,
    UpstreamUnavailableError,
    ValidationError,
)
from suliko.domain.order_files import get_drive_settings, parse_shared_drive_id
from suliko.domain.portal import (
    MatchReason,
    directory_matches,
    find_portal_translator,
    find_tenant_by_slug,
    search_directory,
)
from suliko.integrations.google_drive import DriveError, DriveNotConfiguredError
from suliko.models.audit import ActorType
from suliko.models.directory import Translator
from suliko.models.drive import DriveSettings, OrderDocumentDriveFolder, OrderDriveFolder
from suliko.models.portal import PortalTranslator, PortalTranslatorLink
from suliko.models.tenant import Tenant, TenantStatus

log = structlog.get_logger()

router = APIRouter(prefix="/portal-admin", tags=["portal-admin"])

SlugPath = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")]
#: ASP.NET Identity ids are GUID strings.
ExternalUserIdPath = Annotated[str, Path(min_length=1, max_length=450, pattern=r"^[A-Za-z0-9-]+$")]


# ── Schemas ─────────────────────────────────────────────────────────────────


class OrganizationRef(BaseModel):
    slug: str
    name: str


class AdminOrganizationOut(BaseModel):
    slug: str
    name: str
    status: TenantStatus
    translator_count: int
    shared_drive_id: str | None
    drive_name: str | None


class DriveInfoOut(BaseModel):
    """What the admin panel tells a bureau to share its drive with."""

    configured: bool
    service_account_email: str | None


class DriveLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A Shared Drive id or a pasted Drive link. Null disconnects the drive.
    shared_drive: str | None = Field(max_length=500)


class LinkOut(BaseModel):
    slug: str
    name: str
    translator_id: int
    #: The bureau's own name for this person, which may differ from suliko.ge.
    translator_name: str | None


class AdminTranslatorOut(BaseModel):
    external_user_id: str
    display_name: str
    phone: str | None
    email: str | None
    is_active: bool
    created_at: datetime
    organizations: list[LinkOut]


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


class AdminTranslatorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=255)
    is_active: bool = True

    @field_validator("display_name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    _optional = field_validator("phone", "email")(_blank_to_none)


class DirectoryEntryOut(BaseModel):
    id: int
    name: str
    phone: str | None
    email: str | None
    is_active: bool
    match: MatchReason | None
    #: Linked to a different suliko.ge account; linking it again is refused.
    linked_to_other_account: bool


class CandidatesOut(BaseModel):
    organization: OrganizationRef
    linked_translator_id: int | None
    matches: list[DirectoryEntryOut]
    search_results: list[DirectoryEntryOut]


class LinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The bureau's existing directory row, or null to create one from the account.
    translator_id: int | None = None


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _tenant(db: AsyncSession, slug: str) -> Tenant:
    tenant = await find_tenant_by_slug(db, slug)
    if tenant is None:
        raise NotFoundError("Organisation not found.")
    return tenant


async def _portal_translator(db: AsyncSession, external_user_id: str) -> PortalTranslator:
    row = await find_portal_translator(db, external_user_id)
    if row is None:
        raise NotFoundError("That account is not set up as a translator.")
    return row


async def _audit(
    db: AsyncSession,
    identity: PortalIdentity,
    *,
    action: str,
    entity_type: str,
    entity_id: int | None,
    tenant_id: int | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    from suliko.core.audit import record

    await record(
        db,
        None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        tenant_id=tenant_id,
        actor_type=ActorType.PORTAL_ADMIN,
        before=before,
        after={**(after or {}), "actor_external_user_id": identity.user_id},
    )


async def _translators_out(
    db: AsyncSession, tenants: TenantSessions, rows: Sequence[PortalTranslator]
) -> list[AdminTranslatorOut]:
    if not rows:
        return []

    link_rows = (
        await db.execute(
            select(PortalTranslatorLink, Tenant)
            .join(Tenant, Tenant.id == PortalTranslatorLink.tenant_id)
            .where(PortalTranslatorLink.portal_translator_id.in_([r.id for r in rows]))
            .order_by(Tenant.display_name)
        )
    ).all()

    # Directory names live in each bureau's own table: one query per bureau,
    # inside its scope, rather than one per link.
    wanted: dict[int, set[int]] = defaultdict(set)
    for link, _tenant_row in link_rows:
        wanted[link.tenant_id].add(link.translator_id)
    names: dict[tuple[int, int], str] = {}
    for tenant_id, translator_ids in wanted.items():
        async with tenants(tenant_id) as tenant_db:
            for entry in (
                await tenant_db.execute(select(Translator).where(Translator.id.in_(translator_ids)))
            ).scalars():
                names[(tenant_id, entry.id)] = entry.name

    links: dict[int, list[LinkOut]] = defaultdict(list)
    for link, tenant in link_rows:
        links[link.portal_translator_id].append(
            LinkOut(
                slug=tenant.slug,
                name=tenant.display_name,
                translator_id=link.translator_id,
                translator_name=names.get((tenant.id, link.translator_id)),
            )
        )

    return [
        AdminTranslatorOut(
            external_user_id=row.external_user_id,
            display_name=row.display_name,
            phone=row.phone,
            email=row.email,
            is_active=row.is_active,
            created_at=row.created_at,
            organizations=links[row.id],
        )
        for row in rows
    ]


# ── Organisations and their drives ──────────────────────────────────────────


@router.get("/drive", response_model=DriveInfoOut)
async def get_drive_info(_: PortalAdmin, drive: Drive) -> DriveInfoOut:
    email = drive.service_account_email
    return DriveInfoOut(configured=email is not None, service_account_email=email)


@router.get("/organizations", response_model=list[AdminOrganizationOut])
async def list_organizations(
    _: PortalAdmin, db: PlatformDb, tenants: TenantSessions
) -> list[AdminOrganizationOut]:
    tenants_rows = (await db.execute(select(Tenant).order_by(Tenant.display_name))).scalars().all()

    counts: dict[int, int] = {}
    for tenant_id, count in (
        await db.execute(
            select(PortalTranslatorLink.tenant_id, func.count()).group_by(
                PortalTranslatorLink.tenant_id
            )
        )
    ).all():
        counts[int(tenant_id)] = int(count)

    result: list[AdminOrganizationOut] = []
    for tenant in tenants_rows:
        async with tenants(tenant.id) as tenant_db:
            settings = await get_drive_settings(tenant_db)
        result.append(
            AdminOrganizationOut(
                slug=tenant.slug,
                name=tenant.display_name,
                status=tenant.status,
                translator_count=counts.get(tenant.id, 0),
                shared_drive_id=settings.shared_drive_id if settings else None,
                drive_name=settings.drive_name if settings else None,
            )
        )
    return result


@router.put("/organizations/{slug}/drive", response_model=AdminOrganizationOut)
async def set_organization_drive(
    slug: SlugPath,
    payload: DriveLinkIn,
    identity: PortalAdmin,
    db: PlatformDb,
    tenants: TenantSessions,
    drive: Drive,
) -> AdminOrganizationOut:
    """Connect, change or disconnect a bureau's Shared Drive.

    The drive is opened before it is saved, so a typo or a drive that was never
    shared with Suliko is caught here rather than at a translator's first upload.
    """
    tenant = await _tenant(db, slug)

    drive_id: str | None = None
    drive_name: str | None = None
    if payload.shared_drive is not None:
        drive_id = parse_shared_drive_id(payload.shared_drive)
        if drive_id is None:
            raise ValidationError("That is not a Shared Drive id or link.")
        try:
            drive_name = await drive.get_shared_drive_name(drive_id)
        except DriveNotConfiguredError as exc:
            raise ValidationError(
                "Google Drive is not configured on the API server (GOOGLE_SERVICE_ACCOUNT_FILE)."
            ) from exc
        except DriveError as exc:
            if exc.status in (403, 404):
                raise ValidationError(
                    "Suliko cannot open that Shared Drive. Add "
                    f"{drive.service_account_email} to it as a Content manager, then try again."
                ) from exc
            log.warning("drive_call_failed", status=exc.status, error=str(exc))
            raise UpstreamUnavailableError("Google Drive is not available right now.") from exc

    async with tenants(tenant.id) as tenant_db:
        settings = await get_drive_settings(tenant_db)
        previous = settings.shared_drive_id if settings else None

        if drive_id is None:
            if settings is not None:
                await tenant_db.delete(settings)
        elif settings is None:
            tenant_db.add(DriveSettings(shared_drive_id=drive_id, drive_name=drive_name))
        else:
            settings.shared_drive_id = drive_id
            settings.drive_name = drive_name

        if previous != drive_id:
            # Folder ids point into the previous drive and mean nothing in a new
            # one. Loaded and deleted through the ORM, so the tenant filter —
            # which covers SELECTs, not bulk DELETEs — decides what goes.
            for model in (OrderDocumentDriveFolder, OrderDriveFolder):
                for stale in (await tenant_db.execute(select(model))).scalars():
                    await tenant_db.delete(stale)

        await tenant_db.flush()

    await _audit(
        db,
        identity,
        action="portal.organization_drive_changed",
        entity_type="tenant",
        entity_id=tenant.id,
        tenant_id=tenant.id,
        before={"shared_drive_id": previous},
        after={"shared_drive_id": drive_id, "drive_name": drive_name},
    )

    count = await db.scalar(
        select(func.count())
        .select_from(PortalTranslatorLink)
        .where(PortalTranslatorLink.tenant_id == tenant.id)
    )
    return AdminOrganizationOut(
        slug=tenant.slug,
        name=tenant.display_name,
        status=tenant.status,
        translator_count=int(count or 0),
        shared_drive_id=drive_id,
        drive_name=drive_name,
    )


# ── Translators ─────────────────────────────────────────────────────────────


@router.get("/translators", response_model=list[AdminTranslatorOut])
async def list_translators(
    _: PortalAdmin, db: PlatformDb, tenants: TenantSessions
) -> list[AdminTranslatorOut]:
    rows = (
        (await db.execute(select(PortalTranslator).order_by(PortalTranslator.display_name)))
        .scalars()
        .all()
    )
    return await _translators_out(db, tenants, rows)


@router.get("/translators/{external_user_id}", response_model=AdminTranslatorOut)
async def get_translator(
    external_user_id: ExternalUserIdPath,
    _: PortalAdmin,
    db: PlatformDb,
    tenants: TenantSessions,
) -> AdminTranslatorOut:
    row = await _portal_translator(db, external_user_id)
    return (await _translators_out(db, tenants, [row]))[0]


@router.put("/translators/{external_user_id}", response_model=AdminTranslatorOut)
async def upsert_translator(
    external_user_id: ExternalUserIdPath,
    payload: AdminTranslatorIn,
    identity: PortalAdmin,
    db: PlatformDb,
    tenants: TenantSessions,
) -> AdminTranslatorOut:
    """Mark a suliko.ge account as a translator, or update / deactivate one.

    The name, phone and email come from the suliko.ge user list the admin picked
    the account from; they are what a new directory row is seeded with.
    """
    row = await find_portal_translator(db, external_user_id)
    values = payload.model_dump()
    before: dict[str, Any] | None = None

    if row is None:
        row = PortalTranslator(external_user_id=external_user_id, **values)
        db.add(row)
        action = "portal.translator_added"
    else:
        before = {key: getattr(row, key) for key in values}
        for key, value in values.items():
            setattr(row, key, value)
        action = "portal.translator_updated"

    await db.flush()
    await db.refresh(row)
    await _audit(
        db,
        identity,
        action=action,
        entity_type="portal_translator",
        entity_id=row.id,
        tenant_id=None,
        before=before,
        after=values,
    )
    return (await _translators_out(db, tenants, [row]))[0]


@router.get(
    "/translators/{external_user_id}/organizations/{slug}/candidates",
    response_model=CandidatesOut,
)
async def directory_candidates(
    external_user_id: ExternalUserIdPath,
    slug: SlugPath,
    _: PortalAdmin,
    db: PlatformDb,
    tenants: TenantSessions,
    search: Annotated[str | None, Query(max_length=100)] = None,
) -> CandidatesOut:
    """The bureau's directory rows this account could be linked to.

    ``matches`` share the account's phone or email — usually the same person,
    already in the bureau's books with their history. ``search_results`` let the
    admin pick someone whose details differ.
    """
    translator = await _portal_translator(db, external_user_id)
    tenant = await _tenant(db, slug)

    # A platform table, so the tenant is named explicitly here; the ORM filter
    # only ever adds it to TenantScoped models.
    links = (
        (
            await db.execute(
                select(PortalTranslatorLink).where(PortalTranslatorLink.tenant_id == tenant.id)
            )
        )
        .scalars()
        .all()
    )
    owner_of_row = {link.translator_id: link.portal_translator_id for link in links}
    current = next(
        (link.translator_id for link in links if link.portal_translator_id == translator.id), None
    )

    async with tenants(tenant.id) as tenant_db:
        matches = await directory_matches(tenant_db, phone=translator.phone, email=translator.email)
        found = (
            await search_directory(tenant_db, search.strip()) if search and search.strip() else []
        )

    def entry(row: Translator, reason: MatchReason | None) -> DirectoryEntryOut:
        owner = owner_of_row.get(row.id)
        return DirectoryEntryOut(
            id=row.id,
            name=row.name,
            phone=row.phone,
            email=row.email,
            is_active=row.is_active,
            match=reason,
            linked_to_other_account=owner is not None and owner != translator.id,
        )

    reasons: dict[int, MatchReason] = {row.id: reason for row, reason in matches}
    return CandidatesOut(
        organization=OrganizationRef(slug=tenant.slug, name=tenant.display_name),
        linked_translator_id=current,
        matches=[entry(row, reason) for row, reason in matches],
        search_results=[entry(row, reasons.get(row.id)) for row in found],
    )


@router.put(
    "/translators/{external_user_id}/organizations/{slug}", response_model=AdminTranslatorOut
)
async def link_organization(
    external_user_id: ExternalUserIdPath,
    slug: SlugPath,
    payload: LinkIn,
    identity: PortalAdmin,
    db: PlatformDb,
    tenants: TenantSessions,
) -> AdminTranslatorOut:
    """Link an account to a bureau, through an existing or a new directory row.

    Re-linking to a different row replaces the old link: an account has at most
    one row per bureau.
    """
    translator = await _portal_translator(db, external_user_id)
    tenant = await _tenant(db, slug)

    if payload.translator_id is not None:
        taken = (
            await db.execute(
                select(PortalTranslatorLink).where(
                    PortalTranslatorLink.tenant_id == tenant.id,
                    PortalTranslatorLink.translator_id == payload.translator_id,
                )
            )
        ).scalar_one_or_none()
        if taken is not None and taken.portal_translator_id != translator.id:
            raise ConflictError(
                "That directory entry is already linked to another suliko.ge account."
            )

    created = False
    async with tenants(tenant.id) as tenant_db:
        if payload.translator_id is not None:
            directory_row = await tenant_db.get(Translator, payload.translator_id)
            if directory_row is None:
                # Another bureau's row is indistinguishable from a missing one.
                raise NotFoundError("That translator is not in this organisation's directory.")
        else:
            directory_row = Translator(
                name=translator.display_name,
                phone=translator.phone,
                email=translator.email,
                is_active=True,
                comment="Added from the suliko.ge admin panel for a translator portal account.",
            )
            tenant_db.add(directory_row)
            await tenant_db.flush()
            created = True
        directory_row_id = directory_row.id

    link = (
        await db.execute(
            select(PortalTranslatorLink).where(
                PortalTranslatorLink.portal_translator_id == translator.id,
                PortalTranslatorLink.tenant_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    previous = link.translator_id if link else None
    if link is None:
        db.add(
            PortalTranslatorLink(
                portal_translator_id=translator.id,
                tenant_id=tenant.id,
                translator_id=directory_row_id,
            )
        )
    else:
        link.translator_id = directory_row_id
    await db.flush()

    await _audit(
        db,
        identity,
        action="portal.translator_linked",
        entity_type="translator",
        entity_id=directory_row_id,
        tenant_id=tenant.id,
        before={"translator_id": previous} if previous is not None else None,
        after={
            "portal_translator": translator.external_user_id,
            "translator_id": directory_row_id,
            "created_directory_entry": created,
        },
    )
    return (await _translators_out(db, tenants, [translator]))[0]


@router.delete(
    "/translators/{external_user_id}/organizations/{slug}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
async def unlink_organization(
    external_user_id: ExternalUserIdPath,
    slug: SlugPath,
    identity: PortalAdmin,
    db: PlatformDb,
) -> None:
    """Remove a bureau from a translator's portal.

    The bureau's directory row and its assignments are untouched; relinking
    brings everything back.
    """
    translator = await _portal_translator(db, external_user_id)
    tenant = await _tenant(db, slug)
    link = (
        await db.execute(
            select(PortalTranslatorLink).where(
                PortalTranslatorLink.portal_translator_id == translator.id,
                PortalTranslatorLink.tenant_id == tenant.id,
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise NotFoundError("That account is not linked to this organisation.")

    await db.delete(link)
    await _audit(
        db,
        identity,
        action="portal.translator_unlinked",
        entity_type="translator",
        entity_id=link.translator_id,
        tenant_id=tenant.id,
        before={"portal_translator": translator.external_user_id},
    )
