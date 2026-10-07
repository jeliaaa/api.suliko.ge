"""Order files: rows in ``order_files``, bytes in object storage.

## Layout

    tenants/<tenant id>/orders/<order id>/documents/<document id>/<public id>

The key carries the tenant and order so the bucket reads sensibly to a person
looking at it, and so one bureau's files can be exported or removed with a
single prefix. It deliberately carries NO file name: names are what people type,
they live in the row, and keeping them out of the key means no encoding rules,
no length limits and no collisions to think about.

## Files in the Order Vault

With ``STORAGE_BACKEND=vault`` the bytes go to the vault, which cannot be read
back; ``storage_key`` is then ``vault/<vault order>/<file id>``. So that the
translator assigned to an order can still get its files, a plain working copy is
also written to ordinary storage under the key above and recorded in
``working_key``. Downloads are served from that copy while it exists;
``purge_working_copies`` deletes it when the order closes (or after a cap), and
the vault goes on holding the encrypted archive.

Every function here takes a TENANT-SCOPED session. The rows are tenant data,
and the order and document passed in must already have been loaded inside that
tenant's scope — this module never looks anything up by a caller's id alone.

## A file id is never trusted on its own

A file is always looked up by its public id AND the document it is claimed to
belong to, inside the tenant's scope. A guessed or leaked id from another
document — or another bureau — is simply not found.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.core.errors import NotFoundError
from suliko.domain.statuses import CLOSED_STATUSES, normalise
from suliko.integrations.object_storage import (
    ObjectStorage,
    StorageDownloadUnavailableError,
    StorageError,
    VaultStorage,
    vault_order_of,
)
from suliko.models.order import Order, OrderDocument, OrderStatusEvent
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind

#: Public ids are 22 URL-safe characters; the wider pattern is what the
#: frontends and the old Drive ids already used, so their routes still match.
FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,100}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def new_public_id() -> str:
    return secrets.token_urlsafe(16)


def storage_key(*, tenant_id: int, order_id: int, document_id: int, public_id: str) -> str:
    return f"tenants/{tenant_id}/orders/{order_id}/documents/{document_id}/{public_id}"


def _clean_name(value: str, limit: int = 200) -> str:
    return _CONTROL_CHARACTERS.sub("", value).strip()[:limit]


def safe_file_name(name: str | None) -> str:
    """The last path segment of an uploaded name, without control characters.

    Browsers send only a base name, but the multipart field is caller-controlled
    and the name ends up in a Content-Disposition header.
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


# ── Reading ─────────────────────────────────────────────────────────────────


async def list_document_files(db: AsyncSession, document_id: int) -> list[OrderFile]:
    """The document's live files, oldest first."""
    return list(
        (
            await db.execute(
                select(OrderFile)
                .where(
                    OrderFile.order_document_id == document_id,
                    OrderFile.deleted_at.is_(None),
                )
                .order_by(OrderFile.id)
            )
        )
        .scalars()
        .all()
    )


async def get_document_file(db: AsyncSession, document_id: int, public_id: str) -> OrderFile:
    """The file, if it is a live file of this document. Otherwise 404 — never 403."""
    if not FILE_ID_PATTERN.fullmatch(public_id):
        raise NotFoundError("File not found.")
    row = (
        await db.execute(
            select(OrderFile).where(
                OrderFile.public_id == public_id,
                OrderFile.order_document_id == document_id,
                OrderFile.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("File not found.")
    return row


async def open_download(storage: ObjectStorage, file: OrderFile) -> AsyncIterator[bytes]:
    """The file's bytes, with the first chunk already fetched.

    Fetched up front so that a missing object or a storage outage surfaces as
    an ordinary error response. Once a ``StreamingResponse`` has sent its
    headers, the only way left to report a failure is a cut connection.
    """
    order = vault_order(file)
    if order is not None and isinstance(storage, VaultStorage):
        # In the vault, which cannot be read back. A translator's working copy
        # is the only thing there is to serve, and only while the order is open.
        if file.working_key is None:
            raise StorageDownloadUnavailableError("no working copy of a vault file", order)
        stream = storage.iter_working(file.working_key)
    else:
        stream = storage.iter_get(file.storage_key)
    try:
        first = await anext(stream)
    except StopAsyncIteration:
        first = b""
    except StorageError as exc:
        if order is not None and exc.is_not_found:
            # The copy is gone (cleaned up early, or lost): the vault still has the file.
            raise StorageDownloadUnavailableError("the working copy is gone", order) from exc
        raise

    async def rest() -> AsyncIterator[bytes]:
        if first:
            yield first
        async for chunk in stream:
            yield chunk

    return rest()


# ── Writing ─────────────────────────────────────────────────────────────────


async def upload_document_file(
    db: AsyncSession,
    storage: ObjectStorage,
    *,
    order: Order,
    document: OrderDocument,
    kind: FileKind,
    file_name: str,
    content: bytes,
    content_type: str,
    uploaded_by: str,
) -> OrderFile:
    """Store the bytes, then record the row.

    In that order so a storage failure leaves nothing behind. The reverse
    failure — stored, then the transaction does not commit — leaves an object
    no row points at; it is rare, harmless, and costs a few megabytes.
    """
    public_id = new_public_id()
    key = storage_key(
        tenant_id=order.tenant_id,
        order_id=order.id,
        document_id=document.id,
        public_id=public_id,
    )
    working_key: str | None = None
    if isinstance(storage, VaultStorage):
        # The working copy first: it is the plain-storage write, and if it
        # fails nothing has gone to the vault yet. Both are made, or neither.
        if storage.keeps_working_copies:
            await storage.put_working(key, content, content_type)
            working_key = key
        try:
            # The vault picks the final key (its own order number and file id),
            # and groups an order's files in one vault order.
            key = await storage.put_file(
                key,
                content,
                content_type,
                file_name=file_name,
                kind=kind.value,
                sibling_key=await _vault_sibling_key(db, order.id),
            )
        except StorageError:
            if working_key is not None:
                with suppress(StorageError):
                    await storage.delete_working(working_key)
            raise
    else:
        await storage.put(key, content, content_type)

    row = OrderFile(
        public_id=public_id,
        order_document_id=document.id,
        kind=kind,
        file_name=file_name,
        content_type=content_type,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_key=key,
        working_key=working_key,
        uploaded_by=uploaded_by,
    )
    db.add(row)
    await db.flush()
    return row


async def _vault_sibling_key(db: AsyncSession, order_id: int) -> str | None:
    """A vault key of any file of this order, removed ones included — they
    still sit in the vault until purged — so the next file joins that vault
    order. None for the order's first file."""
    return (
        await db.execute(
            select(OrderFile.storage_key)
            .join(OrderDocument, OrderDocument.id == OrderFile.order_document_id)
            .where(OrderDocument.order_id == order_id, OrderFile.storage_key.like("vault/%"))
            .order_by(OrderFile.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def vault_order(file: OrderFile) -> int | None:
    """The Order Vault's own order number, for a file kept there."""
    return vault_order_of(file.storage_key)


def downloadable(file: OrderFile) -> bool:
    """Whether the API can serve this file: it is in ordinary storage, or it is
    in the vault and still has a working copy."""
    return vault_order(file) is None or file.working_key is not None


async def remove_document_file(db: AsyncSession, file: OrderFile, *, removed_by: str) -> None:
    """Hide the file now; ``purge_removed_files`` deletes the bytes later."""
    file.deleted_at = datetime.now(UTC)
    file.deleted_by = removed_by
    await db.flush()


async def remove_files_of_documents(
    db: AsyncSession, document_ids: Iterable[int], *, removed_by: str
) -> int:
    """Mark every live file of these documents removed, before they are deleted.

    Loaded and updated through the ORM, so the tenant filter decides which
    rows are touched. Returns how many were.
    """
    ids = list(document_ids)
    if not ids:
        return 0
    rows = (
        (
            await db.execute(
                select(OrderFile).where(
                    OrderFile.order_document_id.in_(ids),
                    OrderFile.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for row in rows:
        row.deleted_at = now
        row.deleted_by = removed_by
    await db.flush()
    return len(rows)


# ── Housekeeping ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PurgeResult:
    purged: int
    purged_bytes: int
    failed: int


async def purge_removed_files(
    db: AsyncSession, storage: ObjectStorage, *, cutoff: datetime
) -> PurgeResult:
    """Delete the bytes and rows of files removed before ``cutoff``.

    Run per tenant (the session must be scoped to one). A row whose document
    vanished without the handler marking it — a direct SQL delete, say — is
    marked now, so it gets the same grace period as everything else.

    A file whose object cannot be deleted keeps its row and is retried on the
    next run: deleting the row first would orphan the bytes for good.
    """
    now = datetime.now(UTC)
    for orphan in (
        await db.execute(
            select(OrderFile).where(
                OrderFile.order_document_id.is_(None), OrderFile.deleted_at.is_(None)
            )
        )
    ).scalars():
        orphan.deleted_at = now
        orphan.deleted_by = "system:document-deleted"

    purged = purged_bytes = failed = 0
    due = (
        (
            await db.execute(
                select(OrderFile).where(
                    OrderFile.deleted_at.is_not(None), OrderFile.deleted_at < cutoff
                )
            )
        )
        .scalars()
        .all()
    )
    for row in due:
        try:
            if row.working_key is not None and isinstance(storage, VaultStorage):
                await storage.delete_working(row.working_key)
            await storage.delete(row.storage_key)
        except StorageError:
            failed += 1
            continue
        purged += 1
        purged_bytes += row.size_bytes
        await db.delete(row)
    await db.flush()
    return PurgeResult(purged=purged, purged_bytes=purged_bytes, failed=failed)


async def purge_working_copies(
    db: AsyncSession, storage: ObjectStorage, *, older_than: datetime
) -> PurgeResult:
    """Delete translators' working copies that are no longer needed.

    A copy goes when its file was removed, its document is gone, its order is
    closed (delivered, cancelled or rejected), or it is older than
    ``older_than`` — the cap on how long a plain copy may sit beside the
    encrypted archive. The vault keeps the file either way; the row keeps its
    ``storage_key``, only ``working_key`` is cleared.

    Run per tenant (the session must be scoped to one). A copy that cannot be
    deleted keeps its ``working_key`` and is retried on the next run.
    """
    if not isinstance(storage, VaultStorage):
        return PurgeResult(purged=0, purged_bytes=0, failed=0)

    rows = (
        await db.execute(
            select(OrderFile, OrderDocument.order_id)
            .outerjoin(OrderDocument, OrderDocument.id == OrderFile.order_document_id)
            .where(OrderFile.working_key.is_not(None))
        )
    ).all()
    closed = await _closed_orders(db, {order_id for _, order_id in rows if order_id is not None})

    purged = purged_bytes = failed = 0
    for row, order_id in rows:
        created = row.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        due = (
            row.deleted_at is not None
            or order_id is None
            or order_id in closed
            or (created is not None and created < older_than)
        )
        if not due or row.working_key is None:
            continue
        try:
            await storage.delete_working(row.working_key)
        except StorageError:
            failed += 1
            continue
        row.working_key = None
        purged += 1
        purged_bytes += row.size_bytes
    await db.flush()
    return PurgeResult(purged=purged, purged_bytes=purged_bytes, failed=failed)


async def _closed_orders(db: AsyncSession, order_ids: set[int]) -> set[int]:
    """Which of these orders are in a closed status now.

    An order's status is the latest event in its append-only history. Read here
    in Python rather than with ``DISTINCT ON`` so this runs on any database the
    tests use; only orders that still hold working copies are asked about.
    """
    if not order_ids:
        return set()
    events = await db.execute(
        select(OrderStatusEvent.order_id, OrderStatusEvent.status)
        .where(OrderStatusEvent.order_id.in_(order_ids))
        .order_by(OrderStatusEvent.order_id, OrderStatusEvent.changed_at, OrderStatusEvent.id)
    )
    latest: dict[int, str] = {}
    for order_id, status in events:
        latest[order_id] = normalise(status)
    return {order_id for order_id, status in latest.items() if status in CLOSED_STATUSES}


async def storage_usage(db: AsyncSession) -> tuple[int, int]:
    """(files, bytes) held for the tenant in scope, removed-but-not-purged included.

    Removed files still occupy storage until they are purged, so they count.
    """
    count, total = (
        await db.execute(
            select(func.count(OrderFile.id), func.coalesce(func.sum(OrderFile.size_bytes), 0))
        )
    ).one()
    return int(count), int(total)
