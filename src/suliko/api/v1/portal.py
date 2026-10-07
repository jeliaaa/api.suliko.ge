"""The translator portal: what a translator sees in suliko.ge's Orders tab.

Two kinds of order, kept apart on purpose:

- **Assigned** — documents in a bureau's order that the bureau assigned to this
  translator's directory row. Read one bureau at a time, inside that bureau's
  tenant scope. The translator sees the order id, client name and due date, and
  ONLY the documents assigned to them: never prices, other documents, or other
  translators. Files live in Suliko's order file storage.
- **Personal** — orders the translator created for themselves. Platform-level,
  visible to no bureau, files stored in the database.

Every route resolves the caller from the signed assertion to an *active*
``PortalTranslator``. A bureau is addressed by slug, which only ever selects
among the caller's own links: an unlinked slug is 404, exactly like another
tenant's resource.

File routes also accept a ticket (see ``security/portal_tokens.py``) so a
browser can move large files without passing them through the Next.js server.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, datetime
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, File, Path, UploadFile
from fastapi import status as http_status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from suliko.api.portal_deps import (
    PlatformDb,
    PortalCaller,
    PortalFileCaller,
    PortalIdentity,
    Storage,
    TenantSessions,
)
from suliko.api.v1._files import read_upload
from suliko.api.v1.order_files import storage_failure
from suliko.config import get_settings
from suliko.core.errors import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from suliko.domain.order_files import (
    attachment_headers,
    get_document_file,
    list_document_files,
    open_download,
    remove_document_file,
    safe_content_type,
    safe_file_name,
    upload_document_file,
)
from suliko.domain.portal import (
    AssignedDocument,
    AssignedOrder,
    LinkedOrganization,
    assigned_orders,
    find_portal_translator,
    linked_organizations,
    resolve_pending_invites,
)
from suliko.integrations.object_storage import StorageError
from suliko.models.audit import ActorType
from suliko.models.order_file import OrderFile
from suliko.models.portal import (
    FileKind,
    PersonalOrder,
    PersonalOrderFile,
    PersonalOrderLanguagePair,
    PortalTranslator,
)

log = structlog.get_logger()

router = APIRouter(prefix="/portal", tags=["portal"])

SlugPath = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")]
FileIdPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{8,100}$")]

MAX_LANGUAGE_PAIRS = 20


# ── Schemas ─────────────────────────────────────────────────────────────────


class OrganizationOut(BaseModel):
    slug: str
    name: str


class PortalMe(BaseModel):
    """Drives whether suliko.ge shows the Orders tab at all."""

    is_translator: bool
    display_name: str | None = None
    organizations: list[OrganizationOut] = Field(default_factory=list)


class AssignedDocumentOut(BaseModel):
    id: int
    document_type_name: str | None
    source_language: str
    target_language: str
    page_count: int


class AssignedOrderOut(BaseModel):
    organization: OrganizationOut
    order_id: int
    client_name: str
    order_date: date
    due_date: date | None
    documents: list[AssignedDocumentOut]


class OrderFileOut(BaseModel):
    id: str
    name: str
    kind: FileKind
    content_type: str
    size_bytes: int | None
    created_at: datetime | None
    #: Only these can be removed from the portal.
    uploaded_by_me: bool


#: ok — listed, and uploads work; unavailable — listed, but file storage is
#: not configured on the server, so nothing can be uploaded or downloaded.
#: ("not_linked" belonged to the Google Drive era: there is nothing left for
#: a bureau to link.)
FilesState = Literal["ok", "unavailable"]


class AssignedDocumentDetail(AssignedDocumentOut):
    files_state: FilesState
    files: list[OrderFileOut]


class AssignedOrderDetail(BaseModel):
    organization: OrganizationOut
    order_id: int
    client_name: str
    order_date: date
    due_date: date | None
    documents: list[AssignedDocumentDetail]


class LanguagePairIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_language: str = Field(
        min_length=2, max_length=5, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z]{2})?$"
    )
    target_language: str = Field(
        min_length=2, max_length=5, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z]{2})?$"
    )


class LanguagePairOut(BaseModel):
    source_language: str
    target_language: str


def _require_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


class PersonalOrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_name: str = Field(min_length=1, max_length=255)
    due_date: date | None = None
    notes: str | None = Field(default=None, max_length=5000)
    language_pairs: list[LanguagePairIn] = Field(min_length=1, max_length=MAX_LANGUAGE_PAIRS)

    _client_name = field_validator("client_name")(_require_text)


class PersonalOrderUpdate(BaseModel):
    """Partial update. Omitted fields are left alone; ``language_pairs``
    replaces the whole set when present."""

    model_config = ConfigDict(extra="forbid")

    client_name: str | None = Field(default=None, min_length=1, max_length=255)
    due_date: date | None = None
    notes: str | None = Field(default=None, max_length=5000)
    language_pairs: list[LanguagePairIn] | None = Field(
        default=None, min_length=1, max_length=MAX_LANGUAGE_PAIRS
    )

    _client_name = field_validator("client_name")(_require_text)


class PersonalFileOut(BaseModel):
    id: int
    name: str
    kind: FileKind
    content_type: str
    size_bytes: int
    created_at: datetime


class PersonalOrderSummary(BaseModel):
    id: int
    client_name: str
    due_date: date | None
    created_at: datetime
    language_pairs: list[LanguagePairOut]
    source_file_count: int
    translation_file_count: int


class PersonalOrderDetail(BaseModel):
    id: int
    client_name: str
    due_date: date | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    language_pairs: list[LanguagePairOut]
    files: list[PersonalFileOut]


# ── Helpers ─────────────────────────────────────────────────────────────────


def _uploader_tag(identity: PortalIdentity) -> str:
    return f"portal:{identity.user_id}"


async def _active_translator(db: AsyncSession, identity: PortalIdentity) -> PortalTranslator:
    translator = await find_portal_translator(db, identity.user_id)
    if translator is None or not translator.is_active:
        raise PermissionDeniedError("Your account is not set up as a translator.")
    return translator


async def _organization(
    db: AsyncSession, identity: PortalIdentity, slug: str
) -> LinkedOrganization:
    translator = await _active_translator(db, identity)
    for organization in await linked_organizations(db, translator.id):
        if organization.slug == slug:
            return organization
    # Unlinked, suspended or nonexistent: indistinguishable on purpose.
    raise NotFoundError("Order not found.")


def _organization_out(organization: LinkedOrganization) -> OrganizationOut:
    return OrganizationOut(slug=organization.slug, name=organization.name)


async def _assigned_order(
    tenant_db: AsyncSession, organization: LinkedOrganization, order_id: int
) -> AssignedOrder:
    found = await assigned_orders(tenant_db, organization.translator_id, order_id=order_id)
    if not found:
        raise NotFoundError("Order not found.")
    return found[0]


def _assigned_document(order: AssignedOrder, document_id: int) -> AssignedDocument:
    for item in order.documents:
        if item.document.id == document_id:
            return item
    raise NotFoundError("Document not found.")


def _document_out(item: AssignedDocument) -> AssignedDocumentOut:
    return AssignedDocumentOut(
        id=item.document.id,
        document_type_name=item.document_type_name,
        source_language=item.document.source_language,
        target_language=item.document.target_language,
        page_count=item.document.page_count,
    )


def _file_out(row: OrderFile, identity: PortalIdentity) -> OrderFileOut:
    return OrderFileOut(
        id=row.public_id,
        name=row.file_name,
        kind=row.kind,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
        uploaded_by_me=row.uploaded_by == _uploader_tag(identity),
    )


# ── Who am I ────────────────────────────────────────────────────────────────


@router.get("/me", response_model=PortalMe)
async def get_me(identity: PortalCaller, db: PlatformDb, tenants: TenantSessions) -> PortalMe:
    """Never 403: a user who is not a translator simply gets no Orders tab.

    Also where a bureau's invite catches up with an account that already
    existed when the invite was written — `resolve_pending_invites` runs here
    because this is the one moment we know a translator is present and active.
    A GET that can write is unusual; it is idempotent, it is one indexed
    SELECT on the common case of nothing pending, and there is no other signal
    to hang this on short of a scheduler.
    """
    translator = await find_portal_translator(db, identity.user_id)
    if translator is None or not translator.is_active:
        return PortalMe(is_translator=False)
    await resolve_pending_invites(db, tenants, translator, actor_type=ActorType.PORTAL_TRANSLATOR)
    organizations = await linked_organizations(db, translator.id)
    return PortalMe(
        is_translator=True,
        display_name=translator.display_name,
        organizations=[_organization_out(o) for o in organizations],
    )


# ── Assigned orders ─────────────────────────────────────────────────────────


@router.get("/assignments", response_model=list[AssignedOrderOut])
async def list_assignments(
    identity: PortalCaller, db: PlatformDb, tenants: TenantSessions
) -> list[AssignedOrderOut]:
    translator = await _active_translator(db, identity)
    result: list[AssignedOrderOut] = []

    for organization in await linked_organizations(db, translator.id):
        async with tenants(organization.tenant_id) as tenant_db:
            orders = await assigned_orders(tenant_db, organization.translator_id)
        result.extend(
            AssignedOrderOut(
                organization=_organization_out(organization),
                order_id=order.order.id,
                client_name=order.client_name,
                order_date=order.order.order_date,
                due_date=order.order.due_date,
                documents=[_document_out(item) for item in order.documents],
            )
            for order in orders
        )

    # Order ids come from one sequence across bureaus, so id order is age order.
    result.sort(key=lambda o: (o.order_date, o.order_id), reverse=True)
    return result


@router.get("/organizations/{slug}/orders/{order_id}", response_model=AssignedOrderDetail)
async def get_assigned_order(
    slug: SlugPath,
    order_id: int,
    identity: PortalCaller,
    db: PlatformDb,
    tenants: TenantSessions,
    storage: Storage,
) -> AssignedOrderDetail:
    """One order, with the caller's documents and their files.

    The lists come from the database, so they never wait on storage.
    """
    organization = await _organization(db, identity, slug)
    state: FilesState = "ok" if storage.configured else "unavailable"

    async with tenants(organization.tenant_id) as tenant_db:
        order = await _assigned_order(tenant_db, organization, order_id)

        documents: list[AssignedDocumentDetail] = []
        for item in order.documents:
            files = [
                _file_out(row, identity)
                for row in await list_document_files(tenant_db, item.document.id)
            ]
            documents.append(
                AssignedDocumentDetail(
                    **_document_out(item).model_dump(), files_state=state, files=files
                )
            )

    return AssignedOrderDetail(
        organization=_organization_out(organization),
        order_id=order.order.id,
        client_name=order.client_name,
        order_date=order.order.order_date,
        due_date=order.order.due_date,
        documents=documents,
    )


@router.post(
    "/organizations/{slug}/orders/{order_id}/documents/{document_id}/files",
    response_model=OrderFileOut,
    status_code=http_status.HTTP_201_CREATED,
)
async def upload_translation(
    slug: SlugPath,
    order_id: int,
    document_id: int,
    file: Annotated[UploadFile, File()],
    identity: PortalFileCaller,
    db: PlatformDb,
    tenants: TenantSessions,
    storage: Storage,
) -> OrderFileOut:
    """Upload a translated version of the document.

    Translators upload translations only; source files come from the bureau.
    """
    organization = await _organization(db, identity, slug)
    content = await read_upload(file, get_settings().order_file_max_bytes)

    async with tenants(organization.tenant_id) as tenant_db:
        order = await _assigned_order(tenant_db, organization, order_id)
        item = _assigned_document(order, document_id)
        try:
            uploaded = await upload_document_file(
                tenant_db,
                storage,
                order=order.order,
                document=item.document,
                kind=FileKind.TRANSLATION,
                file_name=safe_file_name(file.filename),
                content=content,
                content_type=safe_content_type(file.content_type),
                uploaded_by=_uploader_tag(identity),
            )
        except StorageError as exc:
            raise storage_failure(exc) from exc

        from suliko.core.audit import record

        await record(
            tenant_db,
            None,
            action="order.file_uploaded",
            entity_type="order",
            entity_id=order_id,
            tenant_id=organization.tenant_id,
            actor_type=ActorType.PORTAL_TRANSLATOR,
            after={
                "document_id": document_id,
                "kind": FileKind.TRANSLATION.value,
                "file_name": uploaded.file_name,
                "actor_external_user_id": identity.user_id,
            },
        )

    return _file_out(uploaded, identity)


@router.get("/organizations/{slug}/orders/{order_id}/documents/{document_id}/files/{file_id}")
async def download_order_file(
    slug: SlugPath,
    order_id: int,
    document_id: int,
    file_id: FileIdPath,
    identity: PortalFileCaller,
    db: PlatformDb,
    tenants: TenantSessions,
    storage: Storage,
) -> StreamingResponse:
    organization = await _organization(db, identity, slug)

    async with tenants(organization.tenant_id) as tenant_db:
        order = await _assigned_order(tenant_db, organization, order_id)
        item = _assigned_document(order, document_id)
        row = await get_document_file(tenant_db, item.document.id, file_id)

    try:
        body = await open_download(storage, row)
    except StorageError as exc:
        raise storage_failure(exc) from exc

    return StreamingResponse(
        body,
        media_type=safe_content_type(row.content_type),
        headers={**attachment_headers(row.file_name), "Content-Length": str(row.size_bytes)},
    )


@router.delete(
    "/organizations/{slug}/orders/{order_id}/documents/{document_id}/files/{file_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
async def delete_order_file(
    slug: SlugPath,
    order_id: int,
    document_id: int,
    file_id: FileIdPath,
    identity: PortalCaller,
    db: PlatformDb,
    tenants: TenantSessions,
) -> None:
    """Remove a translation the caller uploaded.

    Anything else — source files, or files the bureau added — belongs to the
    bureau and is removed in Suliko Office, not from the portal.
    """
    organization = await _organization(db, identity, slug)

    async with tenants(organization.tenant_id) as tenant_db:
        order = await _assigned_order(tenant_db, organization, order_id)
        item = _assigned_document(order, document_id)
        row = await get_document_file(tenant_db, item.document.id, file_id)
        if row.kind is not FileKind.TRANSLATION or row.uploaded_by != _uploader_tag(identity):
            raise PermissionDeniedError("You can only remove translations you uploaded.")
        await remove_document_file(tenant_db, row, removed_by=_uploader_tag(identity))

        from suliko.core.audit import record

        await record(
            tenant_db,
            None,
            action="order.file_removed",
            entity_type="order",
            entity_id=order_id,
            tenant_id=organization.tenant_id,
            actor_type=ActorType.PORTAL_TRANSLATOR,
            before={
                "document_id": document_id,
                "file_name": row.file_name,
                "actor_external_user_id": identity.user_id,
            },
        )


# ── Personal orders ─────────────────────────────────────────────────────────


def _unique_pairs(pairs: list[LanguagePairIn]) -> list[tuple[str, str]]:
    """Lower-cased and de-duplicated, in the order given."""
    unique: dict[tuple[str, str], None] = {}
    for pair in pairs:
        source, target = pair.source_language.lower(), pair.target_language.lower()
        if source == target:
            raise ValidationError("A language pair needs two different languages.")
        unique.setdefault((source, target), None)
    return list(unique)


async def _owned_personal_order(
    db: AsyncSession, translator: PortalTranslator, order_id: int
) -> PersonalOrder:
    row = await db.get(PersonalOrder, order_id)
    if row is None or row.portal_translator_id != translator.id:
        # Someone else's personal order is as absent as a nonexistent one.
        raise NotFoundError("Order not found.")
    return row


def _personal_file_out(row: PersonalOrderFile) -> PersonalFileOut:
    return PersonalFileOut(
        id=row.id,
        name=row.file_name,
        kind=row.kind,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        created_at=row.created_at,
    )


async def _personal_detail(db: AsyncSession, row: PersonalOrder) -> PersonalOrderDetail:
    pairs = (
        (
            await db.execute(
                select(PersonalOrderLanguagePair)
                .where(PersonalOrderLanguagePair.personal_order_id == row.id)
                .order_by(PersonalOrderLanguagePair.id)
            )
        )
        .scalars()
        .all()
    )
    files = (
        (
            await db.execute(
                select(PersonalOrderFile)
                .where(PersonalOrderFile.personal_order_id == row.id)
                .order_by(PersonalOrderFile.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return PersonalOrderDetail(
        id=row.id,
        client_name=row.client_name,
        due_date=row.due_date,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
        language_pairs=[
            LanguagePairOut(source_language=p.source_language, target_language=p.target_language)
            for p in pairs
        ],
        files=[_personal_file_out(f) for f in files],
    )


@router.get("/personal-orders", response_model=list[PersonalOrderSummary])
async def list_personal_orders(
    identity: PortalCaller, db: PlatformDb
) -> list[PersonalOrderSummary]:
    translator = await _active_translator(db, identity)
    orders = (
        (
            await db.execute(
                select(PersonalOrder)
                .where(PersonalOrder.portal_translator_id == translator.id)
                .order_by(PersonalOrder.id.desc())
            )
        )
        .scalars()
        .all()
    )
    if not orders:
        return []
    ids = [o.id for o in orders]

    pairs: dict[int, list[LanguagePairOut]] = defaultdict(list)
    for pair in (
        await db.execute(
            select(PersonalOrderLanguagePair)
            .where(PersonalOrderLanguagePair.personal_order_id.in_(ids))
            .order_by(PersonalOrderLanguagePair.id)
        )
    ).scalars():
        pairs[pair.personal_order_id].append(
            LanguagePairOut(
                source_language=pair.source_language, target_language=pair.target_language
            )
        )

    counts: dict[tuple[int, FileKind], int] = {}
    for order_id, kind, count in (
        await db.execute(
            select(PersonalOrderFile.personal_order_id, PersonalOrderFile.kind, func.count())
            .where(PersonalOrderFile.personal_order_id.in_(ids))
            .group_by(PersonalOrderFile.personal_order_id, PersonalOrderFile.kind)
        )
    ).all():
        counts[(order_id, FileKind(kind))] = int(count)

    return [
        PersonalOrderSummary(
            id=o.id,
            client_name=o.client_name,
            due_date=o.due_date,
            created_at=o.created_at,
            language_pairs=pairs[o.id],
            source_file_count=counts.get((o.id, FileKind.SOURCE), 0),
            translation_file_count=counts.get((o.id, FileKind.TRANSLATION), 0),
        )
        for o in orders
    ]


@router.post(
    "/personal-orders",
    response_model=PersonalOrderDetail,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_personal_order(
    payload: PersonalOrderCreate, identity: PortalCaller, db: PlatformDb
) -> PersonalOrderDetail:
    translator = await _active_translator(db, identity)
    pairs = _unique_pairs(payload.language_pairs)

    row = PersonalOrder(
        portal_translator_id=translator.id,
        client_name=payload.client_name,
        due_date=payload.due_date,
        notes=payload.notes,
    )
    db.add(row)
    await db.flush()
    db.add_all(
        PersonalOrderLanguagePair(personal_order_id=row.id, source_language=s, target_language=t)
        for s, t in pairs
    )
    await db.flush()
    # Server-side timestamps are not loaded by the INSERT; fetch them here
    # rather than letting a lazy load fire outside the async context.
    await db.refresh(row)
    return await _personal_detail(db, row)


@router.get("/personal-orders/{order_id}", response_model=PersonalOrderDetail)
async def get_personal_order(
    order_id: int, identity: PortalCaller, db: PlatformDb
) -> PersonalOrderDetail:
    translator = await _active_translator(db, identity)
    return await _personal_detail(db, await _owned_personal_order(db, translator, order_id))


@router.patch("/personal-orders/{order_id}", response_model=PersonalOrderDetail)
async def update_personal_order(
    order_id: int, payload: PersonalOrderUpdate, identity: PortalCaller, db: PlatformDb
) -> PersonalOrderDetail:
    translator = await _active_translator(db, identity)
    row = await _owned_personal_order(db, translator, order_id)
    changes = payload.model_fields_set

    if "client_name" in changes:
        if payload.client_name is None:
            raise ValidationError("The client name cannot be empty.")
        row.client_name = payload.client_name
    if "due_date" in changes:
        row.due_date = payload.due_date
    if "notes" in changes:
        row.notes = payload.notes
    if "language_pairs" in changes:
        if payload.language_pairs is None:
            raise ValidationError("An order needs at least one language pair.")
        pairs = _unique_pairs(payload.language_pairs)
        await db.execute(
            delete(PersonalOrderLanguagePair).where(
                PersonalOrderLanguagePair.personal_order_id == row.id
            )
        )
        db.add_all(
            PersonalOrderLanguagePair(
                personal_order_id=row.id, source_language=s, target_language=t
            )
            for s, t in pairs
        )

    await db.flush()
    await db.refresh(row)
    return await _personal_detail(db, row)


@router.delete("/personal-orders/{order_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_personal_order(order_id: int, identity: PortalCaller, db: PlatformDb) -> None:
    translator = await _active_translator(db, identity)
    row = await _owned_personal_order(db, translator, order_id)
    # Children explicitly, not only via ON DELETE CASCADE, so the files' bytes
    # are gone in the same statement batch whatever the database's FK settings.
    await db.execute(delete(PersonalOrderFile).where(PersonalOrderFile.personal_order_id == row.id))
    await db.execute(
        delete(PersonalOrderLanguagePair).where(
            PersonalOrderLanguagePair.personal_order_id == row.id
        )
    )
    await db.delete(row)


@router.post(
    "/personal-orders/{order_id}/files",
    response_model=PersonalFileOut,
    status_code=http_status.HTTP_201_CREATED,
)
async def upload_personal_file(
    order_id: int,
    file: Annotated[UploadFile, File()],
    identity: PortalFileCaller,
    db: PlatformDb,
    kind: FileKind = FileKind.SOURCE,
) -> PersonalFileOut:
    translator = await _active_translator(db, identity)
    row = await _owned_personal_order(db, translator, order_id)
    content = await read_upload(file, get_settings().personal_file_max_bytes)

    stored = PersonalOrderFile(
        personal_order_id=row.id,
        kind=kind,
        file_name=safe_file_name(file.filename),
        content_type=safe_content_type(file.content_type),
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    db.add(stored)
    await db.flush()
    await db.refresh(stored, attribute_names=["created_at"])
    return _personal_file_out(stored)


@router.get("/personal-orders/{order_id}/files/{file_id}")
async def download_personal_file(
    order_id: int, file_id: int, identity: PortalFileCaller, db: PlatformDb
) -> Response:
    translator = await _active_translator(db, identity)
    row = await _owned_personal_order(db, translator, order_id)
    stored = (
        await db.execute(
            select(PersonalOrderFile)
            .options(undefer(PersonalOrderFile.content))
            .where(PersonalOrderFile.id == file_id, PersonalOrderFile.personal_order_id == row.id)
        )
    ).scalar_one_or_none()
    if stored is None:
        raise NotFoundError("File not found.")
    return Response(
        content=stored.content,
        media_type=stored.content_type,
        headers=attachment_headers(stored.file_name),
    )


@router.delete(
    "/personal-orders/{order_id}/files/{file_id}", status_code=http_status.HTTP_204_NO_CONTENT
)
async def delete_personal_file(
    order_id: int, file_id: int, identity: PortalCaller, db: PlatformDb
) -> None:
    translator = await _active_translator(db, identity)
    row = await _owned_personal_order(db, translator, order_id)
    result = await db.execute(
        delete(PersonalOrderFile).where(
            PersonalOrderFile.id == file_id, PersonalOrderFile.personal_order_id == row.id
        )
    )
    if not getattr(result, "rowcount", 0):
        raise NotFoundError("File not found.")
