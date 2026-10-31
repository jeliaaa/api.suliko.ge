"""Google Drive as a mirror of an order's files.

Suliko's own storage stays the source of truth: the Claude worker, the Word
editor and every download still read from it. Drive is a copy a bureau can open
in a folder it already knows:

    <root folder>/
      30.10.2026 - #001434 - Client name/
        1. Source Documents/     every source file of the order
        2. Translated/           every translation file (Claude's included)

A bureau with no Drive connected is untouched: nothing here runs for it.

Copying is best effort and never blocks the person who clicked. Files that
could not be copied keep ``drive_file_id`` NULL (and say why in
``drive_error``), and the next sync of the order, the order page's button or
``suliko drive-sync`` picks them up. A file is copied once: replacing the
bytes in Drive when a translation is edited later is out of scope.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import structlog
from sqlalchemy import select

from suliko.core.crypto import DecryptionError, decrypt_for_tenant
from suliko.db.session import session_scope
from suliko.db.tenancy import tenant_scope
from suliko.domain.order_files import open_download
from suliko.integrations.google_drive import DriveError, DriveFolder, GoogleDrive, ServiceAccount
from suliko.integrations.object_storage import ObjectStorage, StorageError, get_object_storage
from suliko.models.directory import Client
from suliko.models.integration import IntegrationCredential, IntegrationProvider
from suliko.models.order import Order, OrderDocument
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind

log = structlog.get_logger()

SOURCE_FOLDER = "1. Source Documents"
TRANSLATED_FOLDER = "2. Translated"

#: Seconds to wait before a sync that was started by a request, so the request's
#: transaction has committed the rows the sync will look for.
START_DELAY = 2.0

_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class Drive(Protocol):
    async def child_folder(self, parent_id: str, name: str) -> DriveFolder: ...

    async def upload_file(
        self, parent_id: str, name: str, content: bytes, content_type: str
    ) -> str: ...


def order_folder_name(*, order_date: date, number: int, client_name: str) -> str:
    """``30.10.2026 - #001434 - Client name``: the shape already in the shared
    drive, so a new order's folder looks like the old ones."""
    client = _FORBIDDEN.sub(" ", client_name or "").strip()
    client = re.sub(r"\s+", " ", client)[:80] or "Client"
    return f"{order_date:%d.%m.%Y} - #{number:06d} - {client}"


@dataclass(frozen=True)
class Connection:
    drive: GoogleDrive
    root_folder_id: str


async def connect(db, tenant_id: int) -> Connection | None:
    """The organisation's Drive, or None when none is connected and enabled."""
    row = (
        await db.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.provider == IntegrationProvider.GOOGLE_DRIVE,
                IntegrationCredential.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None or row.secrets is None:
        return None
    root = str((row.config or {}).get("root_folder_id", "")).strip()
    try:
        raw = json.loads(decrypt_for_tenant(tenant_id, row.secrets)).get("service_account_json")
    except (DecryptionError, ValueError):
        return None
    if not root or not raw:
        return None
    try:
        account = ServiceAccount.parse(
            str(raw), subject=str((row.config or {}).get("impersonate_subject", ""))
        )
    except Exception:  # a malformed key is reported by the settings check
        return None
    return Connection(drive=GoogleDrive(account), root_folder_id=root)


async def ensure_folders(
    drive: Drive, *, root_id: str, order_folder_id: str | None, name: str
) -> tuple[str, str, str]:
    """(order folder, source folder, translated folder) ids, made when missing.

    An order that already has a folder (the Tarjimnebi import brought them
    over) keeps it; only the two sub-folders are looked up or created.
    """
    order_id = order_folder_id or (await drive.child_folder(root_id, name)).id
    source = await drive.child_folder(order_id, SOURCE_FOLDER)
    translated = await drive.child_folder(order_id, TRANSLATED_FOLDER)
    return order_id, source.id, translated.id


@dataclass
class SyncResult:
    connected: bool = False
    copied: int = 0
    failed: int = 0
    folder_id: str | None = None


async def copy_file(
    drive: Drive, storage: ObjectStorage, file: OrderFile, *, source_id: str, translated_id: str
) -> None:
    """Copy one file's bytes into its folder and record the Drive id on the row.
    Raises ``DriveError`` or ``StorageError``."""
    stream = await open_download(storage, file)
    content = b"".join([chunk async for chunk in stream])
    parent = source_id if file.kind is FileKind.SOURCE else translated_id
    file.drive_file_id = await drive.upload_file(parent, file.file_name, content, file.content_type)
    file.drive_error = None


_locks: dict[tuple[int, int], asyncio.Lock] = {}
_tasks: set[asyncio.Task[None]] = set()


async def sync_order(
    tenant_id: int, order_id: int, storage: ObjectStorage | None = None
) -> SyncResult:
    """Make the order's Drive folder match: create it, copy what is missing."""
    result = SyncResult()
    storage = storage or get_object_storage()
    lock = _locks.setdefault((tenant_id, order_id), asyncio.Lock())
    async with lock:
        with tenant_scope(tenant_id):
            async with session_scope() as db:
                connection = await connect(db, tenant_id)
                if connection is None or not storage.configured:
                    return result
                result.connected = True
                try:
                    return await _sync(db, connection, storage, order_id, result)
                except DriveError as exc:
                    log.warning("drive_sync_failed", order=order_id, reason=str(exc))
                    await _remember(db, order_id, str(exc))
                    result.failed += 1
                    return result
                finally:
                    await connection.drive.aclose()


async def _remember(db, order_id: int, reason: str) -> None:
    files = (
        await db.execute(
            select(OrderFile)
            .join(OrderDocument, OrderDocument.id == OrderFile.order_document_id)
            .where(
                OrderDocument.order_id == order_id,
                OrderFile.drive_file_id.is_(None),
                OrderFile.deleted_at.is_(None),
            )
        )
    ).scalars()
    for file in files:
        file.drive_error = reason[:300]


async def _sync(
    db,
    connection: Connection,
    storage: ObjectStorage,
    order_id: int,
    result: SyncResult,
) -> SyncResult:
    # Locked, so a second API process waits here and then finds the folder made.
    order = (
        await db.execute(select(Order).where(Order.id == order_id).with_for_update())
    ).scalar_one_or_none()
    if order is None:
        return result
    client = await db.get(Client, order.client_id)
    name = order_folder_name(
        order_date=order.order_date, number=order.number, client_name=client.name if client else ""
    )
    folder_id, source_id, translated_id = await ensure_folders(
        connection.drive,
        root_id=connection.root_folder_id,
        order_folder_id=order.google_drive_folder,
        name=name,
    )
    order.google_drive_folder = folder_id
    result.folder_id = folder_id
    await db.flush()

    files = (
        (
            await db.execute(
                select(OrderFile)
                .join(OrderDocument, OrderDocument.id == OrderFile.order_document_id)
                .where(
                    OrderDocument.order_id == order_id,
                    OrderFile.drive_file_id.is_(None),
                    OrderFile.deleted_at.is_(None),
                )
                .order_by(OrderFile.id)
                .with_for_update(skip_locked=True, of=OrderFile)
            )
        )
        .scalars()
        .all()
    )
    for file in files:
        try:
            await copy_file(
                connection.drive, storage, file, source_id=source_id, translated_id=translated_id
            )
            result.copied += 1
        except DriveError as exc:
            file.drive_error = str(exc)[:300]
            result.failed += 1
            log.warning("drive_copy_failed", order=order_id, file=file.public_id, reason=str(exc))
            break  # Google is refusing: do not try every remaining file
        except StorageError:
            file.drive_error = "The stored file could not be read."
            result.failed += 1
            log.warning("drive_copy_unreadable", order=order_id, file=file.public_id)
    return result


def schedule_sync(tenant_id: int, order_id: int) -> None:
    """Start a sync in the background and return at once. Never raises."""

    async def run() -> None:
        try:
            await asyncio.sleep(START_DELAY)
            await sync_order(tenant_id, order_id)
        except Exception:
            log.exception("drive_sync_crashed", order=order_id)

    try:
        task = asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
