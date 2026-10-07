"""Order files for bureau staff: the Suliko Office side of a document's Source and
Translation files.

Translators see and add to the same files through the portal
(``api/v1/portal.py``). Both read the ``order_files`` table; the bytes are in
Suliko's object storage — or, with ``STORAGE_BACKEND=vault``, in the Order
Vault, which keeps them encrypted where this API cannot read them back. Those
files are listed (``in_vault``) but a download answers 409; the team opens
them in the vault's own panel.

Tenant-scoped through the normal staff session, like every other Suliko Office route.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Path, UploadFile
from fastapi import status as http_status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.portal_deps import Storage
from suliko.api.v1._files import read_upload
from suliko.config import get_settings
from suliko.core.errors import AppError, ConflictError, NotFoundError, UpstreamUnavailableError
from suliko.domain.order_files import (
    attachment_headers,
    get_document_file,
    list_document_files,
    open_download,
    remove_document_file,
    safe_content_type,
    safe_file_name,
    upload_document_file,
    vault_order,
)
from suliko.integrations.object_storage import (
    StorageDownloadUnavailableError,
    StorageError,
    StorageNotConfiguredError,
)
from suliko.models.order import Order, OrderDocument
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind
from suliko.security.permissions import Permission

log = structlog.get_logger()

router = APIRouter(prefix="/orders", tags=["order-files"])

FileIdPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{8,100}$")]


class StaffFileOut(BaseModel):
    id: str
    name: str
    kind: FileKind
    content_type: str
    size_bytes: int | None
    #: `user:<id>` for a Suliko Office upload, `portal:<id>` for a translator's.
    uploaded_by: str | None
    created_at: datetime | None
    #: Kept in the Order Vault: listed here, opened only in the vault's panel.
    in_vault: bool = False
    #: The vault's own order number for that file, to find it by.
    vault_order: int | None = None


def _out(row: OrderFile) -> StaffFileOut:
    vault_no = vault_order(row)
    return StaffFileOut(
        id=row.public_id,
        name=row.file_name,
        kind=row.kind,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at,
        in_vault=vault_no is not None,
        vault_order=vault_no,
    )


class StoredInVaultError(ConflictError):
    """A download of a file that only the Order Vault's team can open."""

    error_code = "stored_in_vault"


def storage_failure(exc: StorageError) -> AppError:
    if isinstance(exc, StorageDownloadUnavailableError):
        where = f" (vault order {exc.vault_order})" if exc.vault_order else ""
        return StoredInVaultError(
            f"This file is kept in the Order Vault{where}, where only the vault's team can "
            "open it. It cannot be downloaded here.",
            vault_order=exc.vault_order,
        )
    if isinstance(exc, StorageNotConfiguredError):
        log.error("storage_not_configured")
        return UpstreamUnavailableError("File storage is not configured on the server.")
    if exc.is_not_found:
        # A row whose bytes are gone: worth a loud log line, not a 503.
        log.error("storage_object_missing", error=str(exc))
        return NotFoundError("That file is no longer there.")
    log.warning("storage_call_failed", status=exc.status, error=str(exc))
    return UpstreamUnavailableError("File storage is not available right now. Please try again.")


async def _document(
    db: AsyncSession, order_id: int, document_id: int
) -> tuple[Order, OrderDocument]:
    order = await db.get(Order, order_id)
    document = await db.get(OrderDocument, document_id)
    if order is None or document is None or document.order_id != order_id:
        raise NotFoundError("Document not found.")
    return order, document


@router.get("/{order_id}/documents/{document_id}/files", response_model=list[StaffFileOut])
async def list_files(
    order_id: int,
    document_id: int,
    db: Db,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> list[StaffFileOut]:
    await _document(db, order_id, document_id)
    return [_out(row) for row in await list_document_files(db, document_id)]


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
    storage: Storage,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
    kind: FileKind = FileKind.SOURCE,
) -> StaffFileOut:
    order, document = await _document(db, order_id, document_id)
    content = await read_upload(file, get_settings().order_file_max_bytes)
    try:
        row = await upload_document_file(
            db,
            storage,
            order=order,
            document=document,
            kind=kind,
            file_name=safe_file_name(file.filename),
            content=content,
            content_type=safe_content_type(file.content_type),
            uploaded_by=f"user:{session.user_id}",
        )
    except StorageError as exc:
        raise storage_failure(exc) from exc

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.file_uploaded",
        entity_type="order",
        entity_id=order_id,
        after={"document_id": document_id, "kind": kind.value, "file_name": row.file_name},
    )
    return _out(row)


@router.get("/{order_id}/documents/{document_id}/files/{file_id}")
async def download_file(
    order_id: int,
    document_id: int,
    file_id: FileIdPath,
    db: Db,
    storage: Storage,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> StreamingResponse:
    await _document(db, order_id, document_id)
    row = await get_document_file(db, document_id, file_id)
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
    "/{order_id}/documents/{document_id}/files/{file_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
)
async def delete_file(
    order_id: int,
    document_id: int,
    file_id: FileIdPath,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> None:
    """Remove a file. Restorable by support until the retention period ends."""
    await _document(db, order_id, document_id)
    row = await get_document_file(db, document_id, file_id)
    await remove_document_file(db, row, removed_by=f"user:{session.user_id}")

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.file_removed",
        entity_type="order",
        entity_id=order_id,
        before={"document_id": document_id, "kind": row.kind.value, "file_name": row.file_name},
    )
