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

import hashlib
import hmac
import re
from dataclasses import dataclass
from urllib.parse import quote

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.core.errors import NotFoundError
from suliko.db.tenancy import TenantContextError, is_bypassed, try_get_current_tenant_id
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


# ── Linking a bureau to a Shared Drive ──────────────────────────────────────
#
# Shared by the two places that do it: a bureau's own Settings → Integrations,
# and the suliko.ge admin panel. One implementation, so the checks that matter
# cannot drift between them.
#
# ## Why a bureau has to PROVE it owns the drive
#
# Every bureau adds the SAME Suliko service account to its drive, so that
# account can open every linked drive on the platform, and a drive id is all
# it takes to point a tenant at one. Without a proof, bureau A could paste
# bureau B's drive id and read B's documents through the CRM.
#
# The proof is the same shape as domain verification. Each tenant has a fixed
# folder name nobody else can predict (`drive_verification_name`), and the
# drive must contain a folder with that name at its top level. Creating one
# needs write access to the drive — which is precisely what an attacker
# pointing at someone else's drive does not have. Suliko itself never creates
# folders at a drive's top level except `Suliko Orders`, so no request can be
# used to plant the marker on a bureau's behalf.


class DriveLinkError(Exception):
    """A drive could not be linked. `message` is written to be shown."""

    def __init__(self, message: str, *, upstream: bool = False) -> None:
        super().__init__(message)
        self.message = message
        #: True when Google, not the input, is at fault.
        self.upstream = upstream


async def resolve_shared_drive(
    drive: DriveClient, raw: str | None
) -> tuple[str | None, str | None]:
    """Parse a pasted id or link and open the drive, before anything is saved.

    Returns ``(None, None)`` for "disconnect". Opening it first means a typo, or
    a drive nobody shared with Suliko, is caught now rather than at the first
    upload.
    """
    from suliko.integrations.google_drive import DriveNotConfiguredError

    if raw is None or not raw.strip():
        return None, None

    drive_id = parse_shared_drive_id(raw)
    if drive_id is None:
        raise DriveLinkError("That is not a Shared Drive id or link.")

    try:
        name = await drive.get_shared_drive_name(drive_id)
    except DriveNotConfiguredError as exc:
        raise DriveLinkError(
            "Google Drive is not configured on the API server "
            "(GOOGLE_SERVICE_ACCOUNT_FILE is not set)."
        ) from exc
    except DriveError as exc:
        if exc.status in (403, 404):
            raise DriveLinkError(
                "Suliko cannot open that Shared Drive. Add "
                f"{drive.service_account_email} to it as a Content manager, then try again."
            ) from exc
        raise DriveLinkError("Google Drive is not available right now.", upstream=True) from exc

    return drive_id, name


VERIFICATION_PREFIX = "suliko-verify-"


def drive_verification_name(tenant_id: int) -> str:
    """The folder a tenant must create in its drive before linking it.

    Derived, not stored: an HMAC of the tenant id under the server's master
    key. Stable across page loads, so the instructions a bureau reads do not
    change under them — and unguessable from outside, so no bureau can work
    out another's. A leaked value is harmless: it only ever verifies a drive
    for the tenant it was derived from.
    """
    from suliko.config import get_settings

    key = get_settings().encryption_master_key.get_secret_value().encode()
    digest = hmac.new(key, f"suliko-drive-verify:{tenant_id}".encode(), hashlib.sha256)
    return VERIFICATION_PREFIX + digest.hexdigest()[:16]


async def verify_drive_ownership(
    db: AsyncSession, drive: DriveClient, *, drive_id: str, tenant_id: int
) -> None:
    """Refuse a drive this tenant has not proved it controls.

    Two checks, in the order that gives the clearer message:

    1. No OTHER tenant has this drive linked. A drive holds one bureau's
       files; sharing one would mix two bureaus' folders in it.
    2. The drive contains this tenant's verification folder at its top level.

    Check 1 reads across tenants and is therefore only as good as what the
    session can see. It is defence in depth — check 2 is the one that holds on
    its own, because it needs write access to the drive itself.
    """
    from suliko.db.tenancy import bypass_tenant_scope

    with bypass_tenant_scope():
        taken = (
            await db.execute(
                select(DriveSettings.tenant_id).where(
                    DriveSettings.shared_drive_id == drive_id,
                    DriveSettings.tenant_id != tenant_id,
                )
            )
        ).first()
    if taken is not None:
        raise DriveLinkError(
            "That Shared Drive is already connected to another organisation on Suliko."
        )

    marker = drive_verification_name(tenant_id)
    try:
        found = await drive.find_folder(drive_id=drive_id, parent_id=drive_id, name=marker)
    except DriveError as exc:
        raise DriveLinkError("Google Drive is not available right now.", upstream=True) from exc

    if found is None:
        raise DriveLinkError(
            f'Create a folder named "{marker}" at the top level of that Shared Drive, '
            "then try again. It proves the drive is yours; you can delete it once the "
            "drive is connected."
        )


async def save_drive_link(
    db: AsyncSession, drive_id: str | None, drive_name: str | None
) -> str | None:
    """Store the link for the tenant IN SCOPE, and return the previous drive id.

    `db` must already be scoped to the target tenant — ambient context, RLS
    GUC and all. The stale-folder cleanup below relies on the ORM filter to
    pick that tenant's rows, so an unscoped session here would wipe every
    bureau's folder ids at once.
    """
    # Refuse rather than trust the caller. Under `bypass_tenant_scope()` the
    # cleanup below is unfiltered and deletes EVERY bureau's folder ids — and
    # the platform router, the natural caller, holds that bypass for its reads.
    # Failing loudly here turns a silent platform-wide wipe into a stack trace.
    if is_bypassed() or try_get_current_tenant_id() is None:
        raise TenantContextError(
            "save_drive_link needs a single tenant in scope. Wrap the call in "
            "tenant_scope(tenant_id), not bypass_tenant_scope()."
        )

    settings = await get_drive_settings(db)
    previous = settings.shared_drive_id if settings else None

    if drive_id is None:
        if settings is not None:
            await db.delete(settings)
    elif settings is None:
        db.add(DriveSettings(shared_drive_id=drive_id, drive_name=drive_name))
    else:
        settings.shared_drive_id = drive_id
        settings.drive_name = drive_name

    if previous != drive_id:
        # Folder ids point into the previous drive and mean nothing in a new
        # one. Loaded and deleted through the ORM, so the tenant filter —
        # which covers SELECTs, not bulk DELETEs — decides what goes.
        for model in (OrderDocumentDriveFolder, OrderDriveFolder):
            for stale in (await db.execute(select(model))).scalars():
                await db.delete(stale)

    await db.flush()
    return previous


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
