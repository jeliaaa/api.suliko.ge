"""Order files in a bureau's Shared Drive.

## Layout

    <Shared Drive>/
      Suliko Orders/
        #123 · Client name/                  order_drive_folders
          Document 456 · en → ka/            order_document_drive_folders.folder_id
            Source/                          …source_folder_id
            Translation/                     …translation_folder_id

A folder per document because translators are assigned per document and may see
only their own documents' files. Staff drop source files into ``Source``
directly in Drive; translators upload into ``Translation`` through the portal.

Every function here takes a TENANT-SCOPED session. The folder rows are tenant
data, and the order and document passed in must already have been loaded inside
that tenant's scope — this module never looks anything up by a caller's id.

## A Drive file id is never trusted on its own

Callers name files by Drive id. Before a file is served or removed its metadata
is fetched and its parent must be the Source or Translation folder of a document
the caller may see. Without that check, any file the service account can reach —
in any bureau's drive — would be downloadable by anyone holding a portal login.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.core.errors import NotFoundError
from suliko.integrations.google_drive import DriveClient, DriveError, DriveFile
from suliko.models.drive import DriveSettings, OrderDocumentDriveFolder, OrderDriveFolder
from suliko.models.order import Order, OrderDocument
from suliko.models.portal import FileKind

ROOT_FOLDER_NAME = "Suliko Orders"
KIND_FOLDER_NAMES = {FileKind.SOURCE: "Source", FileKind.TRANSLATION: "Translation"}

#: Written onto every file Suliko uploads, so the portal can tell a translator
#: which files are theirs to remove. Files staff add in Drive carry neither.
APP_PROPERTY_KIND = "suliko_kind"
APP_PROPERTY_UPLOADED_BY = "suliko_uploaded_by"

DRIVE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,100}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class DriveNotLinkedError(Exception):
    """This bureau has not linked a Shared Drive yet."""


@dataclass(frozen=True, slots=True)
class DocumentFolders:
    drive_id: str
    folder_id: str
    source_folder_id: str
    translation_folder_id: str

    def folder_for(self, kind: FileKind) -> str:
        return self.source_folder_id if kind is FileKind.SOURCE else self.translation_folder_id

    def kind_of(self, file: DriveFile) -> FileKind | None:
        """Which of this document's folders the file sits in, if either."""
        if self.source_folder_id in file.parents:
            return FileKind.SOURCE
        if self.translation_folder_id in file.parents:
            return FileKind.TRANSLATION
        return None


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def parse_shared_drive_id(value: str) -> str | None:
    """Accept a Shared Drive id or a pasted Drive link. None when neither."""
    value = value.strip()
    match = re.search(r"/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)", value)
    if match:
        value = match.group(1)
    return value if DRIVE_ID_PATTERN.fullmatch(value) else None


def _clean_name(value: str, limit: int = 200) -> str:
    return _CONTROL_CHARACTERS.sub("", value).strip()[:limit]


def safe_file_name(name: str | None) -> str:
    """The last path segment of an uploaded name, without control characters.

    Browsers send only a base name, but the multipart field is caller-controlled
    and the name ends up in Drive and in a Content-Disposition header.
    """
    base = re.split(r"[\\/]", name or "")[-1]
    return _clean_name(base) or "file"


def safe_content_type(value: str | None) -> str:
    value = (value or "").split(";")[0].strip().lower()
    if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", value) or len(value) > 100:
        return "application/octet-stream"
    return value


def attachment_headers(file_name: str) -> dict[str, str]:
    """Headers for serving a stored file.

    Always ``attachment`` with ``nosniff``: uploaded bytes are served from the
    API's own origin, and an HTML or SVG file rendered inline there would be a
    stored-XSS vector against every portal user.
    """
    fallback = file_name.encode("ascii", "ignore").decode("ascii").replace('"', "")
    fallback = fallback.replace("\\", "") or "file"
    return {
        "Content-Disposition": (
            f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(file_name)}"
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }


async def get_drive_settings(db: AsyncSession) -> DriveSettings | None:
    return (await db.execute(select(DriveSettings))).scalars().first()


async def _find_or_create(
    drive: DriveClient, *, drive_id: str, parent_id: str, name: str
) -> DriveFile:
    existing = await drive.find_folder(drive_id=drive_id, parent_id=parent_id, name=name)
    return existing or await drive.create_folder(parent_id=parent_id, name=name)


async def _order_folder_id(
    db: AsyncSession, drive: DriveClient, drive_id: str, order: Order, client_name: str
) -> str:
    stored = (
        await db.execute(select(OrderDriveFolder).where(OrderDriveFolder.order_id == order.id))
    ).scalar_one_or_none()
    if stored is not None:
        return stored.folder_id

    root = await _find_or_create(
        drive, drive_id=drive_id, parent_id=drive_id, name=ROOT_FOLDER_NAME
    )
    folder = await _find_or_create(
        drive,
        drive_id=drive_id,
        parent_id=root.id,
        name=_clean_name(f"#{order.id} · {client_name}"),
    )
    db.add(OrderDriveFolder(order_id=order.id, folder_id=folder.id))
    await db.flush()
    return folder.id


async def ensure_document_folders(
    db: AsyncSession,
    drive: DriveClient,
    *,
    order: Order,
    document: OrderDocument,
    client_name: str,
) -> DocumentFolders:
    """The document's folders, creating whatever is missing.

    The order row is locked first, so two first-time requests for documents of
    the same order cannot both create the order folder. (SQLite ignores the
    lock; the unique constraint still refuses the second row.)
    """
    settings = await get_drive_settings(db)
    if settings is None:
        raise DriveNotLinkedError
    drive_id = settings.shared_drive_id

    await db.execute(select(Order.id).where(Order.id == order.id).with_for_update())

    stored = (
        await db.execute(
            select(OrderDocumentDriveFolder).where(
                OrderDocumentDriveFolder.order_document_id == document.id
            )
        )
    ).scalar_one_or_none()
    if stored is not None:
        return DocumentFolders(
            drive_id=drive_id,
            folder_id=stored.folder_id,
            source_folder_id=stored.source_folder_id,
            translation_folder_id=stored.translation_folder_id,
        )

    name = _clean_name(
        f"Document {document.id} · {document.source_language} → {document.target_language}"
    )
    order_folder_id = await _order_folder_id(db, drive, drive_id, order, client_name)
    try:
        folder = await _find_or_create(
            drive, drive_id=drive_id, parent_id=order_folder_id, name=name
        )
    except DriveError as exc:
        if not exc.is_not_found:
            raise
        # The order folder was deleted in Drive. Forget it and start again.
        await db.execute(delete(OrderDriveFolder).where(OrderDriveFolder.order_id == order.id))
        order_folder_id = await _order_folder_id(db, drive, drive_id, order, client_name)
        folder = await _find_or_create(
            drive, drive_id=drive_id, parent_id=order_folder_id, name=name
        )

    source = await _find_or_create(
        drive, drive_id=drive_id, parent_id=folder.id, name=KIND_FOLDER_NAMES[FileKind.SOURCE]
    )
    translation = await _find_or_create(
        drive,
        drive_id=drive_id,
        parent_id=folder.id,
        name=KIND_FOLDER_NAMES[FileKind.TRANSLATION],
    )
    db.add(
        OrderDocumentDriveFolder(
            order_document_id=document.id,
            folder_id=folder.id,
            source_folder_id=source.id,
            translation_folder_id=translation.id,
        )
    )
    await db.flush()
    return DocumentFolders(
        drive_id=drive_id,
        folder_id=folder.id,
        source_folder_id=source.id,
        translation_folder_id=translation.id,
    )


async def forget_document_folders(db: AsyncSession, document_id: int) -> None:
    """Drop stored folder ids so the next use recreates them."""
    await db.execute(
        delete(OrderDocumentDriveFolder).where(
            OrderDocumentDriveFolder.order_document_id == document_id
        )
    )


async def list_document_files(
    drive: DriveClient, folders: DocumentFolders
) -> list[tuple[FileKind, DriveFile]]:
    files: list[tuple[FileKind, DriveFile]] = []
    for kind in FileKind:
        children = await drive.list_children(
            drive_id=folders.drive_id, folder_id=folders.folder_for(kind)
        )
        files.extend((kind, child) for child in children if not child.is_folder)
    return files


async def authorize_file(
    drive: DriveClient, folders: DocumentFolders, file_id: str
) -> tuple[FileKind, DriveFile]:
    """The file, if it belongs to this document. Otherwise 404 — never 403."""
    if not DRIVE_ID_PATTERN.fullmatch(file_id):
        raise NotFoundError("File not found.")
    try:
        file = await drive.get_file(file_id)
    except DriveError as exc:
        if exc.is_not_found:
            raise NotFoundError("File not found.") from exc
        raise
    kind = folders.kind_of(file)
    if kind is None or file.trashed or file.is_folder:
        raise NotFoundError("File not found.")
    return kind, file


async def upload_document_file(
    db: AsyncSession,
    drive: DriveClient,
    *,
    order: Order,
    document: OrderDocument,
    client_name: str,
    kind: FileKind,
    file_name: str,
    content: bytes,
    content_type: str,
    uploaded_by: str,
) -> DriveFile:
    """Upload into the document's Source or Translation folder.

    A folder deleted in Drive since it was recorded fails the upload with 404;
    the stored ids are then dropped and the upload retried once into fresh
    folders.
    """
    properties = {APP_PROPERTY_KIND: kind.value, APP_PROPERTY_UPLOADED_BY: uploaded_by}
    folders = await ensure_document_folders(
        db, drive, order=order, document=document, client_name=client_name
    )
    try:
        return await drive.upload_file(
            parent_id=folders.folder_for(kind),
            name=file_name,
            content=content,
            content_type=content_type,
            app_properties=properties,
        )
    except DriveError as exc:
        if not exc.is_not_found:
            raise
    await forget_document_folders(db, document.id)
    folders = await ensure_document_folders(
        db, drive, order=order, document=document, client_name=client_name
    )
    return await drive.upload_file(
        parent_id=folders.folder_for(kind),
        name=file_name,
        content=content,
        content_type=content_type,
        app_properties=properties,
    )
