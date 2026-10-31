"""Order files for bureau staff: the Suliko Office side of a document's Source and
Translation files.

Translators see and add to the same files through the portal
(``api/v1/portal.py``). Both read the ``order_files`` table; the bytes are in
Suliko's object storage — or, with ``STORAGE_BACKEND=vault``, in the Order
Vault, which keeps them encrypted where this API cannot read them back. Those
files are listed (``in_vault``) and download from a plain working copy while the
order is open (``downloadable``); once it closes the copy is deleted and a
download answers 409 — the team opens the file in the vault's own panel.

Tenant-scoped through the normal staff session, like every other Suliko Office route.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, Path, UploadFile
from fastapi import status as http_status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.portal_deps import Storage
from suliko.api.v1._files import read_upload
from suliko.config import get_settings
from suliko.core.errors import AppError, ConflictError, NotFoundError, UpstreamUnavailableError
from suliko.domain.order_files import (
    FILE_ID_PATTERN,
    attachment_headers,
    downloadable,
    get_document_file,
    is_editable_translation,
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
from suliko.models.translation import DocumentTranslation, TranslationStatus
from suliko.security.permissions import Permission

log = structlog.get_logger()

router = APIRouter(prefix="/orders", tags=["order-files"])

FileIdPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{8,100}$")]

#: Files linked in one request. An order with more than this many documents
#: is not one a person links by hand.
MAX_LINKS = 200


class StaffFileOut(BaseModel):
    id: str
    name: str
    kind: FileKind
    content_type: str
    size_bytes: int | None
    #: `user:<id>` for a Suliko Office upload, `portal:<id>` for a translator's.
    uploaded_by: str | None
    created_at: datetime | None
    #: Archived in the Order Vault (encrypted; opened in the vault's panel).
    in_vault: bool = False
    #: The vault's own order number for that file, to find it by.
    vault_order: int | None = None
    #: Whether a download here will work. False for a vault file once its
    #: working copy is gone (the order closed): then only the vault has it.
    downloadable: bool = True
    #: Whether the Word editor can open it: see
    #: `domain.order_files.is_editable_translation`.
    editable: bool = False
    #: Copied to the organisation's Google Drive.
    in_drive: bool = False
    #: Why the last copy to Drive failed.
    drive_error: str | None = None


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
        downloadable=downloadable(row),
        editable=is_editable_translation(row),
        in_drive=row.drive_file_id is not None,
        drive_error=row.drive_error,
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


class FileChange(BaseModel):
    """What staff may change about a stored file: its side, and its name."""

    kind: FileKind | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def _something_to_change(self) -> FileChange:
        if self.kind is None and self.name is None:
            raise ValueError("Nothing to change: give a kind, a name, or both.")
        return self


def _renamed(current: str, wanted: str) -> str:
    """`wanted` as a safe name that keeps the extension `current` has.

    The extension is how the person downloading it knows what opens the file,
    and the bytes have not changed: "passport.pdf" renamed to "client scan"
    stays a ".pdf".
    """
    name = safe_file_name(wanted)
    suffix = PurePosixPath(current).suffix
    if suffix and not name.lower().endswith(suffix.lower()):
        name = safe_file_name(f"{name[: 200 - len(suffix)]}{suffix}")
    return name


@router.patch(
    "/{order_id}/documents/{document_id}/files/{file_id}",
    response_model=StaffFileOut,
)
async def update_file(
    order_id: int,
    document_id: int,
    file_id: FileIdPath,
    payload: FileChange,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> StaffFileOut:
    """Move a file between Source and Translation, or rename it.

    A file dropped in the wrong column used to have one remedy: delete it and
    upload it again. Neither change touches the bytes or where they are kept
    (the storage key names the document and the file, not the side), so this
    is a row update and nothing else.

    A file kept in the Order Vault is left as it was filed: the vault holds
    its own record of the name and the side, and this API cannot rewrite it.
    """
    await _document(db, order_id, document_id)
    row = await get_document_file(db, document_id, file_id)
    if vault_order(row) is not None:
        raise ConflictError("This file is kept in the Order Vault, where it stays as it was filed.")

    before = {"document_id": document_id, "kind": row.kind.value, "file_name": row.file_name}
    if payload.kind is not None:
        row.kind = payload.kind
    if payload.name is not None:
        row.file_name = _renamed(row.file_name, payload.name)
    after = {"document_id": document_id, "kind": row.kind.value, "file_name": row.file_name}
    if after == before:
        return _out(row)
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.file_changed",
        entity_type="order",
        entity_id=order_id,
        before=before,
        after=after,
    )
    return _out(row)


class FileLink(BaseModel):
    """One file and the document of the order it is to belong to."""

    file_id: str = Field(pattern=FILE_ID_PATTERN.pattern)
    document_id: int


class LinkFilesIn(BaseModel):
    links: list[FileLink] = Field(min_length=1, max_length=MAX_LINKS)

    @model_validator(mode="after")
    def _each_file_once(self) -> LinkFilesIn:
        if len({link.file_id for link in self.links}) != len(self.links):
            raise ValueError("A file can be linked to one document at a time.")
        return self


class LinkedFileOut(BaseModel):
    id: str
    document_id: int


@router.post("/{order_id}/files/link", response_model=list[LinkedFileOut])
async def link_files(
    order_id: int,
    payload: LinkFilesIn,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> list[LinkedFileOut]:
    """Say which document of the order each file belongs to.

    Documents of one order can have different language pairs, and a file is
    translated (and shown to a translator) as part of the document it is on,
    so a file on the wrong one has to be moved, not deleted and sent again.
    Only the row changes: the bytes keep the key they were stored under, which
    names the document they were uploaded to and is never read back for its
    meaning.

    All or nothing: if any file in the request cannot move, none does, and the
    answer names it. Files already on the document they are asked to go to are
    left alone and left out of the answer.
    """
    order = await db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found.")
    document_ids = set(
        (await db.execute(select(OrderDocument.id).where(OrderDocument.order_id == order_id)))
        .scalars()
        .all()
    )
    if not {link.document_id for link in payload.links} <= document_ids:
        raise NotFoundError("Document not found.")

    rows = {
        row.public_id: row
        for row in (
            await db.execute(
                select(OrderFile).where(
                    OrderFile.public_id.in_([link.file_id for link in payload.links]),
                    OrderFile.order_document_id.in_(document_ids),
                    OrderFile.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    }
    if len(rows) != len(payload.links):
        raise NotFoundError("File not found.")

    moving = [(rows[link.file_id], link.document_id) for link in payload.links]
    moving = [(row, to) for row, to in moving if row.order_document_id != to]
    # A job that is still running will file its result under the document the
    # file was on when it was started.
    busy = set(
        (
            await db.execute(
                select(DocumentTranslation.source_file_id).where(
                    DocumentTranslation.source_file_id.in_([row.id for row, _ in moving]),
                    DocumentTranslation.status == TranslationStatus.PROCESSING,
                )
            )
        )
        .scalars()
        .all()
    )
    for row, _ in moving:
        if row.id in busy:
            raise ConflictError(
                f"Suliko Translate is working on {row.file_name}. Link it when it has finished."
            )
    if not moving:
        return []

    changes = [
        {
            "file_name": row.file_name,
            "from_document_id": row.order_document_id,
            "to_document_id": to,
        }
        for row, to in moving
    ]
    for row, to in moving:
        row.order_document_id = to
    await db.flush()

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.files_linked",
        entity_type="order",
        entity_id=order_id,
        after={"files": changes},
    )
    return [LinkedFileOut(id=row.public_id, document_id=to) for row, to in moving]


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
