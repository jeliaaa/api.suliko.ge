"""Correcting a translation in the Word editor: open it, save it back.

The editor is Syncfusion's Document Editor, embedded in Office's translation
page and in its full-screen page. It works on the Word document itself, so
what a translator sees is what the client receives: pages, tables, headers and
footers, not an approximation of them in HTML.

It runs in the browser and cannot read a .docx, so opening one is a round trip
(`GET .../files/{file_id}/editor`): Office reads the stored file and has
suliko.ge's backend turn it into the editor's format (`integrations/
word_editor.py`). A translation filed as HTML, from before Word files or when a
conversion failed, opens the same way. The editor writes a .docx in the browser,
so saving needs nothing from suliko.ge: `PUT .../files/{file_id}/docx` takes
the bytes, checks they are a Word document (`domain/word_file.py`), files them as
a new file in the old one's place and retires the old one, which support can
still restore for the retention period.

What may be edited is any Translation file that is a Word document or an HTML
file, whoever added it: a translator's own .docx is corrected the same way as
one Suliko Translate made.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.portal_deps import Storage
from suliko.api.v1._files import read_upload
from suliko.api.v1.order_files import FileIdPath, StaffFileOut, _document, _out, storage_failure
from suliko.config import get_settings
from suliko.core.errors import ConflictError, NotFoundError, PayloadTooLargeError
from suliko.domain.order_files import (
    get_document_file,
    is_editable_translation,
    open_download,
    remove_document_file,
    upload_document_file,
)
from suliko.domain.word_file import DOCX_CONTENT_TYPE, ensure_word_document
from suliko.integrations.object_storage import StorageError
from suliko.integrations.word_editor import WordEditorImporter, get_word_importer
from suliko.models.portal import FileKind
from suliko.models.translation import DocumentTranslation
from suliko.security.permissions import Permission

log = structlog.get_logger()

router = APIRouter(prefix="/orders", tags=["order-word-editor"])

Importer = Annotated[WordEditorImporter, Depends(get_word_importer)]


class EditorDocumentOut(BaseModel):
    #: The document as the editor's JSON text, for `documentEditor.open`.
    sfdt: str
    file_name: str


@router.get(
    "/{order_id}/documents/{document_id}/files/{file_id}/editor",
    response_model=EditorDocumentOut,
)
async def open_in_editor(
    order_id: int,
    document_id: int,
    file_id: FileIdPath,
    db: Db,
    storage: Storage,
    importer: Importer,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> EditorDocumentOut:
    """A translation in the form the editor opens."""
    await _document(db, order_id, document_id)
    current = await get_document_file(db, document_id, file_id)
    if not is_editable_translation(current):
        raise NotFoundError("This file cannot be edited here.")
    limit = get_settings().word_editor_max_bytes
    if current.size_bytes > limit:
        raise PayloadTooLargeError("This document is too large to open in the editor.")

    try:
        body = await open_download(storage, current)
        content = b"".join([chunk async for chunk in body])
    except StorageError as exc:
        raise storage_failure(exc) from exc

    extension = "docx" if current.content_type == DOCX_CONTENT_TYPE else "html"
    sfdt = await importer.to_sfdt(file_name=f"translation.{extension}", content=content)
    return EditorDocumentOut(sfdt=sfdt, file_name=current.file_name)


@router.put(
    "/{order_id}/documents/{document_id}/files/{file_id}/docx",
    response_model=StaffFileOut,
)
async def save_word_document(
    order_id: int,
    document_id: int,
    file_id: FileIdPath,
    file: Annotated[UploadFile, File()],
    db: Db,
    session: CurrentSession,
    storage: Storage,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> StaffFileOut:
    """Save the editor's Word document: a new file in the old one's place."""
    order, document = await _document(db, order_id, document_id)
    current = await get_document_file(db, document_id, file_id)
    if current.kind is not FileKind.TRANSLATION or not is_editable_translation(current):
        raise ConflictError("Only a translation can be edited here.")

    content = await read_upload(file, get_settings().word_editor_max_bytes)
    ensure_word_document(content)

    # Always a .docx now, whatever the file it replaces was.
    name = f"{PurePosixPath(current.file_name).stem or 'translation'}.docx"
    try:
        stored = await upload_document_file(
            db,
            storage,
            order=order,
            document=document,
            kind=FileKind.TRANSLATION,
            file_name=name,
            content=content,
            content_type=DOCX_CONTENT_TYPE,
            uploaded_by=f"user:{session.user_id}",
        )
    except StorageError as exc:
        raise storage_failure(exc) from exc
    await remove_document_file(db, current, removed_by=f"user:{session.user_id}")

    # The translation's record follows its file. The HTML it kept is of the
    # translation as it was made, not as it has been corrected: it goes.
    for record in (
        (
            await db.execute(
                select(DocumentTranslation).where(DocumentTranslation.result_file_id == current.id)
            )
        )
        .scalars()
        .all()
    ):
        record.result_file_id = stored.id
        record.html = None
    await db.flush()

    from suliko.core.audit import record as audit

    await audit(
        db,
        session,
        action="order.translation_edited",
        entity_type="order",
        entity_id=order_id,
        after={"document_id": document_id, "file_name": stored.file_name, "editor": "word"},
    )
    return _out(stored)
