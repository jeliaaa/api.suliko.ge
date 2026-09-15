"""Order files for bureau staff: the CRM side of the Shared Drive folders.

Staff can also work in Drive directly — drop a scan into a document's ``Source``
folder and the assigned translator sees it. These routes are for doing the same
from the CRM, and for creating a document's folders ahead of time so there is
somewhere to drop files before the translator first opens the order.

Tenant-scoped through the normal staff session, like every other CRM route.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Path, UploadFile
from fastapi import status as http_status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.portal_deps import Drive
from suliko.api.v1._files import read_upload
from suliko.config import get_settings
from suliko.core.errors import (
    AppError,
    NotFoundError,
    UpstreamUnavailableError,
    ValidationError,
)
from suliko.domain.order_files import (
    DocumentFolders,
    DriveNotLinkedError,
    attachment_headers,
    authorize_file,
    ensure_document_folders,
    folder_url,
    list_document_files,
    safe_content_type,
    safe_file_name,
    upload_document_file,
)
from suliko.integrations.google_drive import DriveError, DriveNotConfiguredError
from suliko.models.directory import Client
from suliko.models.order import Order, OrderDocument
from suliko.models.portal import FileKind
from suliko.security.permissions import Permission

log = structlog.get_logger()

router = APIRouter(prefix="/orders", tags=["order-files"])

DriveFileIdPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{8,100}$")]


class DocumentFoldersOut(BaseModel):
    source_folder_url: str
    translation_folder_url: str


class StaffFileOut(BaseModel):
    id: str
    name: str
    kind: FileKind
    content_type: str
    size_bytes: int | None
    uploaded_by: str | None


def _failure(exc: DriveError) -> AppError:
    if isinstance(exc, DriveNotConfiguredError):
        log.error("drive_not_configured")
        return UpstreamUnavailableError("File storage is not configured on the server.")
    log.warning("drive_call_failed", status=exc.status, error=str(exc))
    return UpstreamUnavailableError("Google Drive is not available right now. Please try again.")


async def _document(
    db: AsyncSession, order_id: int, document_id: int
) -> tuple[Order, OrderDocument, str]:
    order = await db.get(Order, order_id)
    document = await db.get(OrderDocument, document_id)
    if order is None or document is None or document.order_id != order_id:
        raise NotFoundError("Document not found.")
    client = await db.get(Client, order.client_id)
    return order, document, client.name if client else ""


async def _folders(
    db: AsyncSession, drive: Drive, order_id: int, document_id: int
) -> DocumentFolders:
    order, document, client_name = await _document(db, order_id, document_id)
    try:
        return await ensure_document_folders(
            db, drive, order=order, document=document, client_name=client_name
        )
    except DriveNotLinkedError as exc:
        raise ValidationError("This organisation has not connected a Google Drive yet.") from exc
    except DriveError as exc:
        raise _failure(exc) from exc


@router.post("/{order_id}/documents/{document_id}/drive-folders", response_model=DocumentFoldersOut)
async def create_document_folders(
    order_id: int,
    document_id: int,
    db: Db,
    drive: Drive,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> DocumentFoldersOut:
    folders = await _folders(db, drive, order_id, document_id)
    return DocumentFoldersOut(
        source_folder_url=folder_url(folders.source_folder_id),
        translation_folder_url=folder_url(folders.translation_folder_id),
    )


@router.get("/{order_id}/documents/{document_id}/files", response_model=list[StaffFileOut])
async def list_files(
    order_id: int,
    document_id: int,
    db: Db,
    drive: Drive,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> list[StaffFileOut]:
    folders = await _folders(db, drive, order_id, document_id)
    try:
        files = await list_document_files(drive, folders)
    except DriveError as exc:
        raise _failure(exc) from exc
    return [
        StaffFileOut(
            id=file.id,
            name=file.name,
            kind=kind,
            content_type=file.mime_type,
            size_bytes=file.size_bytes,
            uploaded_by=file.app_properties.get("suliko_uploaded_by"),
        )
        for kind, file in files
    ]


@router.post(
    "/{order_id}/documents/{document_id}/files",
    response_model=StaffFileOut,
    status_code=http_status.HTTP_201_CREATED,
)
async def upload_file(
    order_id: int,
    document_id: int,
    file: Annotated[UploadFile, File()],
    db: Db,
    session: CurrentSession,
    drive: Drive,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
    kind: FileKind = FileKind.SOURCE,
) -> StaffFileOut:
    order, document, client_name = await _document(db, order_id, document_id)
    content = await read_upload(file, get_settings().drive_file_max_bytes)
    try:
        uploaded = await upload_document_file(
            db,
            drive,
            order=order,
            document=document,
            client_name=client_name,
            kind=kind,
            file_name=safe_file_name(file.filename),
            content=content,
            content_type=safe_content_type(file.content_type),
            uploaded_by=f"user:{session.user_id}",
        )
    except DriveNotLinkedError as exc:
        raise ValidationError("This organisation has not connected a Google Drive yet.") from exc
    except DriveError as exc:
        raise _failure(exc) from exc

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.file_uploaded",
        entity_type="order",
        entity_id=order_id,
        after={"document_id": document_id, "kind": kind.value, "file_name": uploaded.name},
    )
    return StaffFileOut(
        id=uploaded.id,
        name=uploaded.name,
        kind=kind,
        content_type=uploaded.mime_type,
        size_bytes=uploaded.size_bytes,
        uploaded_by=f"user:{session.user_id}",
    )


@router.get("/{order_id}/documents/{document_id}/files/{file_id}")
async def download_file(
    order_id: int,
    document_id: int,
    file_id: DriveFileIdPath,
    db: Db,
    drive: Drive,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> StreamingResponse:
    folders = await _folders(db, drive, order_id, document_id)
    try:
        _kind, file = await authorize_file(drive, folders, file_id)
    except DriveError as exc:
        raise _failure(exc) from exc
    if file.mime_type.startswith("application/vnd.google-apps."):
        raise ValidationError("This is a Google Docs file; open it in Drive instead.")
    return StreamingResponse(
        drive.iter_download(file.id),
        media_type=safe_content_type(file.mime_type),
        headers=attachment_headers(file.name),
    )


@router.delete(
    "/{order_id}/documents/{document_id}/files/{file_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
async def delete_file(
    order_id: int,
    document_id: int,
    file_id: DriveFileIdPath,
    db: Db,
    session: CurrentSession,
    drive: Drive,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> None:
    """Move a file to the drive's bin (recoverable there for 30 days)."""
    folders = await _folders(db, drive, order_id, document_id)
    try:
        kind, file = await authorize_file(drive, folders, file_id)
        await drive.trash_file(file.id)
    except DriveError as exc:
        raise _failure(exc) from exc

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.file_removed",
        entity_type="order",
        entity_id=order_id,
        before={"document_id": document_id, "kind": kind.value, "file_name": file.name},
    )
