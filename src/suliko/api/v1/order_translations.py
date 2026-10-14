"""Suliko Translate from an order: a document's source file in, its translation out.

One click on a source file starts it (`POST .../files/{file_id}/translate`).
The translator a document is assigned to can do the same from suliko.ge's
Orders tab (`api/v1/portal.py`), with what is here: `start_translation`,
`refresh_translation` and the request and answer shapes.
The work is done on suliko.ge and paid for there, from the page balance of the
person who clicked: Office names them by the suliko.ge account they sign in
with, so there is nothing to set up and nobody else is charged. A person whose
Office account has no suliko.ge account behind it cannot use it, and is told.

There is no background worker here. suliko.ge runs the job; Office asks how it
is doing whenever someone looks (`GET .../translations`, which the order page
polls while a job runs). The first look after it finishes brings the result
over and files it under the document's Translation files, where it is like any
other file: downloadable, removable, sendable.

The result comes back as HTML and is filed as a Word document
(`domain/translation_docx.py`): that is what a client is sent and a translator
finishes in Word. The HTML is kept beside it, on the translation's row, because
it is what the order's translation page edits. The page shows it next to the
source (`GET .../files/{file_id}/html`); `PUT .../files/{file_id}/content`
builds the Word file again from the corrected text, files it as a new file and
retires the one it replaces, which support can still restore for the retention
period. If the HTML cannot be made into a Word file the HTML itself is filed,
so a translation that has been paid for is never lost to a conversion.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.portal_deps import Storage
from suliko.api.v1.order_files import FileIdPath, StaffFileOut, _document, _out, storage_failure
from suliko.config import get_settings
from suliko.core.errors import ConflictError, NotFoundError, PayloadTooLargeError, ValidationError
from suliko.domain.order_files import (
    downloadable,
    get_document_file,
    new_public_id,
    open_download,
    remove_document_file,
    upload_document_file,
)
from suliko.domain.translation_docx import DOCX_CONTENT_TYPE, DocxConversionError, html_to_docx
from suliko.integrations.object_storage import ObjectStorage, StorageError
from suliko.integrations.suliko_backend import SulikoUnavailableError
from suliko.integrations.suliko_translate import (
    InsufficientBalanceError,
    NameSpelling,
    SulikoAccountMissingError,
    SulikoLanguage,
    SulikoTranslator,
    get_suliko_translator,
)
from suliko.models.order import Order, OrderDocument
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind
from suliko.models.reference import Language, TenantSettings
from suliko.models.translation import DocumentTranslation, TranslationStatus
from suliko.models.user import Account, User
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

log = structlog.get_logger()

router = APIRouter(prefix="/orders", tags=["order-translations"])

Translator = Annotated[SulikoTranslator, Depends(get_suliko_translator)]

HTML = "text/html"


class LanguageNotAvailableError(ValidationError):
    """Suliko Translate does not offer the document's target language."""

    error_code = "language_not_available"


#: suliko.ge's own limit on the notes (`DocumentController.MaxInstructionsLength`).
MAX_INSTRUCTIONS = 4000
MAX_NAMES = 100


class NameIn(BaseModel):
    """How one proper name is to be written in the translation."""

    model_config = ConfigDict(extra="forbid")

    original: str = Field(min_length=1, max_length=200)
    translation: str = Field(min_length=1, max_length=200)
    type: Literal["Person", "Organization"] = "Person"


class TranslateIn(BaseModel):
    #: Ask the model to rebuild tables, colours and emphasis. Slower.
    rich: bool = False
    #: The person's own notes for this translation: terms, tone, what to leave
    #: as it is. suliko.ge adds them to its prompt after its own requirements.
    instructions: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS)
    #: Spellings fixed for names, so the model does not choose its own.
    names: list[NameIn] = Field(default_factory=list, max_length=MAX_NAMES)

    @field_validator("instructions")
    @classmethod
    def _no_blank_notes(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @field_validator("names")
    @classmethod
    def _no_blank_names(cls, value: list[NameIn]) -> list[NameIn]:
        """A row of the form left half filled in is dropped, not refused."""
        return [
            name.model_copy(
                update={"original": name.original.strip(), "translation": name.translation.strip()}
            )
            for name in value
            if name.original.strip() and name.translation.strip()
        ]


class SaveContentIn(BaseModel):
    html: str = Field(min_length=1)


class TranslationHtmlOut(BaseModel):
    html: str


class TranslationOut(BaseModel):
    id: str
    document_id: int | None
    status: TranslationStatus
    #: The file that was translated, and the one that came back. Null once
    #: that file has been removed.
    source_file_id: str | None
    result_file_id: str | None
    target_language: str
    #: What suliko.ge measured and charged.
    page_count: int
    #: 0 to 100 while it runs, as suliko.ge reports it.
    progress: int | None = None
    error: str | None
    created_at: datetime | None
    finished_at: datetime | None
    #: On starting one only: what is left of the person's balance, if known.
    balance: Decimal | None = None


async def public_file_ids(db: AsyncSession, rows: list[DocumentTranslation]) -> dict[int, str]:
    ids = {i for row in rows for i in (row.source_file_id, row.result_file_id) if i is not None}
    if not ids:
        return {}
    found = (
        await db.execute(
            select(OrderFile.id, OrderFile.public_id).where(
                OrderFile.id.in_(ids), OrderFile.deleted_at.is_(None)
            )
        )
    ).all()
    return {row[0]: row[1] for row in found}


def translation_out(
    row: DocumentTranslation,
    files: dict[int, str],
    *,
    progress: int | None = None,
    balance: Decimal | None = None,
) -> TranslationOut:
    return TranslationOut(
        id=row.public_id,
        document_id=row.order_document_id,
        status=row.status,
        source_file_id=files.get(row.source_file_id) if row.source_file_id else None,
        result_file_id=files.get(row.result_file_id) if row.result_file_id else None,
        target_language=row.target_language,
        page_count=row.page_count,
        progress=progress if row.status is TranslationStatus.PROCESSING else None,
        error=row.error,
        created_at=row.created_at,
        finished_at=row.finished_at,
        balance=balance,
    )


# ── Which suliko.ge language ────────────────────────────────────────────────


def match_language(
    code: str, name_en: str | None, name_ka: str | None, offered: list[SulikoLanguage]
) -> SulikoLanguage | None:
    """suliko.ge's language for one of Office's.

    suliko.ge knows a language by a number and two names; Office by a code and
    two names of the bureau's choosing. The names are what the two share, so
    they are compared, English first, without regard to case or spacing, and
    without the word suliko.ge adds to most of its own ("English Language",
    "პოლონური ენა").
    """

    def norm(value: str | None) -> str:
        words = (value or "").casefold().split()
        while words and words[-1] in _NAME_SUFFIXES:
            words.pop()
        return " ".join(words)

    wanted = {norm(name_en), norm(name_ka)}
    wanted.update(norm(name) for name in _CODE_NAMES.get(code.lower(), ()))
    wanted.discard("")
    for language in offered:
        if norm(language.name) in wanted or norm(language.name_geo) in wanted:
            return language
    return None


#: What suliko.ge appends to a language's name, in either script.
_NAME_SUFFIXES = frozenset({"language", "ენა"})

#: What a code is called on suliko.ge, for a bureau that renamed its language
#: ("Georgian (formal)") or wrote it in another script. More than one name
#: where suliko.ge spells it its own way ("Finish Language").
_CODE_NAMES: dict[str, tuple[str, ...]] = {
    "ka": ("Georgian",),
    "en": ("English",),
    "ru": ("Russian",),
    "de": ("German",),
    "fr": ("French",),
    "es": ("Spanish",),
    "it": ("Italian",),
    "nl": ("Dutch",),
    "pl": ("Polish",),
    "pt": ("Portuguese",),
    "tr": ("Turkish",),
    "ar": ("Arabic",),
    "uk": ("Ukrainian",),
    "hy": ("Armenian",),
    "az": ("Azerbaijani",),
    "zh": ("Chinese",),
    "he": ("Hebrew",),
    "fa": ("Persian",),
    "el": ("Greek",),
    "ja": ("Japanese",),
    "lv": ("Latvian",),
    "sl": ("Slovenian",),
    "sk": ("Slovak",),
    "sr": ("Serbian",),
    "ur": ("Urdu",),
    "fi": ("Finnish", "Finish"),
    "la": ("Latin",),
    "ro": ("Romanian",),
}


async def _suliko_language(
    db: AsyncSession, code: str, offered: list[SulikoLanguage]
) -> SulikoLanguage | None:
    own = (await db.execute(select(Language).where(Language.code == code))).scalars().first()
    return match_language(code, own.name_en if own else None, own.name_ka if own else None, offered)


async def _suliko_user_id(db: AsyncSession, session: AuthenticatedSession) -> str:
    """The suliko.ge account of the person asking: whose balance will pay."""
    user = await db.get(User, session.user_id)
    account = await db.get(Account, user.account_id) if user and user.account_id else None
    if account is None or not account.suliko_user_id:
        raise SulikoAccountMissingError(
            "Suliko Translate uses your suliko.ge account, and this sign-in has none. "
            "Sign in to Suliko Office through suliko.ge to use it."
        )
    return account.suliko_user_id


# ── Starting one ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Started:
    row: DocumentTranslation
    out: TranslationOut
    #: False when the click found the same file already being translated.
    is_new: bool

    def audit(self, document_id: int, source: OrderFile) -> dict[str, object]:
        return {
            "document_id": document_id,
            "file_name": source.file_name,
            "pages": self.row.page_count,
            "target_language": self.row.target_language,
        }


async def start_translation(
    db: AsyncSession,
    storage: ObjectStorage,
    translator: SulikoTranslator,
    *,
    document: OrderDocument,
    source: OrderFile,
    payload: TranslateIn,
    suliko_user_id: Callable[[], Awaitable[str]],
    requested_by_user_id: int | None = None,
    requested_by_portal_user_id: str | None = None,
) -> Started:
    """Hand one file of a document to Suliko Translate, for whoever asked.

    Shared by Office (`translate_file`) and the translator portal
    (`portal.translate_assigned_file`): the caller has already established
    that the person may touch this document, and says whose suliko.ge balance
    pays. `suliko_user_id` is asked for only once the file is known to be
    worth sending, so a person without an account is not told so about a file
    that could not have been translated anyway.
    """
    # A second click while the first is still running gets the same job, not
    # a second charge.
    running = (
        (
            await db.execute(
                select(DocumentTranslation).where(
                    DocumentTranslation.source_file_id == source.id,
                    DocumentTranslation.status == TranslationStatus.PROCESSING,
                )
            )
        )
        .scalars()
        .first()
    )
    if running is not None:
        out = translation_out(running, await public_file_ids(db, [running]))
        return Started(running, out, is_new=False)

    settings = get_settings()
    if not downloadable(source):
        raise ValidationError(
            "This file is kept in the Order Vault and cannot be translated from here."
        )
    if source.size_bytes > settings.suliko_translate_max_bytes:
        limit_mb = settings.suliko_translate_max_bytes // (1024 * 1024)
        raise PayloadTooLargeError(
            f"Suliko Translate takes files up to {limit_mb} MB. This one is larger."
        )

    payer = await suliko_user_id()

    offered = await translator.languages()
    target = await _suliko_language(db, document.target_language, offered)
    if target is None:
        raise LanguageNotAvailableError(
            "Suliko Translate does not offer this document's target language yet.",
            language=document.target_language,
        )
    source_language = await _suliko_language(db, document.source_language, offered)
    # The language of the review notes suliko.ge writes for the reader: the
    # bureau's own, in whichever of the two it has.
    settings_row = (await db.execute(select(TenantSettings))).scalars().first()
    reader = match_language(
        "en" if settings_row and settings_row.default_language == "en" else "ka",
        None,
        None,
        offered,
    )

    try:
        body = await open_download(storage, source)
        content = b"".join([chunk async for chunk in body])
    except StorageError as exc:
        raise storage_failure(exc) from exc

    prepared = await translator.prepare(
        payer,
        file_name=source.file_name,
        content_type=source.content_type,
        content=content,
    )
    if prepared.balance is not None and prepared.balance < prepared.page_count:
        raise InsufficientBalanceError(
            "Your suliko.ge balance does not cover this document.",
            pages=prepared.page_count,
            balance=str(prepared.balance),
        )
    job_id = await translator.start(
        payer,
        prepared,
        file_name=source.file_name,
        target_language_id=target.id,
        source_language_id=source_language.id if source_language else None,
        output_language_id=(reader or target).id,
        rich=payload.rich,
        instructions=payload.instructions,
        names=[NameSpelling(n.original, n.translation, n.type) for n in payload.names],
    )

    row = DocumentTranslation(
        public_id=new_public_id(),
        order_document_id=document.id,
        source_file_id=source.id,
        status=TranslationStatus.PROCESSING,
        suliko_job_id=job_id,
        suliko_user_id=payer,
        requested_by_user_id=requested_by_user_id,
        requested_by_portal_user_id=requested_by_portal_user_id,
        target_language=document.target_language,
        page_count=prepared.page_count,
    )
    db.add(row)
    await db.flush()
    out = translation_out(
        row,
        await public_file_ids(db, [row]),
        progress=0,
        balance=prepared.balance - prepared.page_count if prepared.balance is not None else None,
    )
    return Started(row, out, is_new=True)


@router.post(
    "/{order_id}/documents/{document_id}/files/{file_id}/translate",
    response_model=TranslationOut,
    status_code=http_status.HTTP_201_CREATED,
)
async def translate_file(
    order_id: int,
    document_id: int,
    file_id: FileIdPath,
    payload: TranslateIn,
    db: Db,
    session: CurrentSession,
    storage: Storage,
    translator: Translator,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> TranslationOut:
    _order, document = await _document(db, order_id, document_id)
    source = await get_document_file(db, document_id, file_id)
    started = await start_translation(
        db,
        storage,
        translator,
        document=document,
        source=source,
        payload=payload,
        suliko_user_id=lambda: _suliko_user_id(db, session),
        requested_by_user_id=session.user_id,
    )
    if started.is_new:
        from suliko.core.audit import record

        await record(
            db,
            session,
            action="order.translation_started",
            entity_type="order",
            entity_id=order_id,
            after=started.audit(document_id, source),
        )
    return started.out


# ── Following them ──────────────────────────────────────────────────────────


# ── The file that is kept ───────────────────────────────────────────────────

UTF8_META = '<meta charset="utf-8">'

#: Any declaration of a character set, in either of the two ways HTML has.
_CHARSET_META = re.compile(r"<meta\b[^>]*\bcharset\s*=[^>]*>", re.IGNORECASE)
_DOCTYPE = re.compile(r"\s*<!doctype\b", re.IGNORECASE)
_HTML_OPEN = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_HEAD_OPEN = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_BOM = "﻿"


def html_document(html: str) -> str:
    """The translation as a whole HTML document that says it is UTF-8.

    suliko.ge sends a fragment (a `<div>` and what is in it) and the page that
    corrects it sends a document with an empty head. Neither says which
    character set it is in. Shown from Office that does not matter, the
    response says so; but the file is also downloaded and emailed to clients,
    and opened from a disk or a mail preview with nothing to go by, Georgian
    comes out as garbage in a viewer that does not guess.

    The file is always stored as UTF-8, so a declaration of anything else is
    wrong by now and is replaced. Running this on its own output changes
    nothing.
    """
    text = _CHARSET_META.sub("", html.lstrip(_BOM))
    if head := _HEAD_OPEN.search(text):
        text = text[: head.end()] + UTF8_META + text[head.end() :]
    elif root := _HTML_OPEN.search(text):
        text = f"{text[: root.end()]}<head>{UTF8_META}</head>{text[root.end() :]}"
    else:
        text = f"<html><head>{UTF8_META}</head><body>\n{text.strip()}\n</body></html>"
    return text if _DOCTYPE.match(text) else f"<!doctype html>\n{text}"


def _result_stem(source_name: str | None, target_language: str) -> str:
    stem = PurePosixPath(source_name or "translation").stem or "translation"
    return f"{stem[:180]} ({target_language.upper()})"


@dataclass(frozen=True, slots=True)
class TranslationFile:
    """A translation ready to be filed."""

    name: str
    content: bytes
    content_type: str
    #: The HTML to keep beside a Word file, to be corrected and built again.
    #: None when the file is the HTML itself, or when it was too large to keep.
    html: str | None


async def make_translation_file(
    html: str, *, stem: str, language: str, word: bool = True
) -> TranslationFile:
    """The translation as the file to store: a Word document, or, when that
    cannot be made (or is not wanted), the HTML.

    Never raises for a document it cannot convert: the translation has been
    paid for, and an HTML file the person can still read and correct is better
    than an error in its place.
    """
    text = html_document(html)
    if word:
        try:
            content = await asyncio.to_thread(html_to_docx, text, language=language)
        except DocxConversionError as exc:
            log.warning("translation_docx_failed", error=str(exc))
        else:
            fits = len(text.encode("utf-8")) <= get_settings().translation_html_max_bytes
            return TranslationFile(
                f"{stem}.docx", content, DOCX_CONTENT_TYPE, text if fits else None
            )
    return TranslationFile(f"{stem}.html", text.encode("utf-8"), HTML, None)


def result_uploader(row: DocumentTranslation) -> str:
    """Whose file a finished translation is.

    One a translator started from the portal is theirs, tagged the way their
    own uploads are, so they see it as such and can remove it. One started in
    Office is Suliko Translate's, on behalf of the person who clicked.
    """
    if row.requested_by_portal_user_id:
        return f"portal:{row.requested_by_portal_user_id}"
    return f"suliko:{row.requested_by_user_id or 0}"


async def _collect(
    db: AsyncSession,
    storage: ObjectStorage,
    translator: SulikoTranslator,
    row: DocumentTranslation,
    order: Order,
    document: OrderDocument,
) -> None:
    """Bring a finished job's result over and file it under the document.

    Claimed first, with an update that only one request can win: two people
    looking at the order at the moment a job finishes must not file it twice.
    """
    # RETURNING rather than a row count, which the async cursor does not give.
    claimed = (
        await db.execute(
            update(DocumentTranslation)
            .where(
                DocumentTranslation.id == row.id,
                DocumentTranslation.status == TranslationStatus.PROCESSING,
            )
            .values(status=TranslationStatus.COMPLETED)
            .returning(DocumentTranslation.id)
        )
    ).first()
    if claimed is None:
        await db.refresh(row)
        return

    result = await translator.result(row.suliko_job_id)
    source = await db.get(OrderFile, row.source_file_id) if row.source_file_id else None
    # Whatever suliko.ge labels it, what it sends is the HTML it built, in UTF-8.
    made = await make_translation_file(
        result.content.decode("utf-8-sig", errors="replace"),
        stem=_result_stem(source.file_name if source else None, row.target_language),
        language=row.target_language,
    )
    try:
        stored = await upload_document_file(
            db,
            storage,
            order=order,
            document=document,
            kind=FileKind.TRANSLATION,
            file_name=made.name,
            content=made.content,
            content_type=made.content_type,
            uploaded_by=result_uploader(row),
        )
    except StorageError as exc:
        raise storage_failure(exc) from exc
    row.status = TranslationStatus.COMPLETED
    row.result_file_id = stored.id
    row.html = made.html
    row.finished_at = datetime.now(UTC)
    await db.flush()


async def refresh_translation(
    db: AsyncSession,
    storage: ObjectStorage,
    translator: SulikoTranslator,
    row: DocumentTranslation,
    order: Order,
    document: OrderDocument,
) -> int | None:
    """Ask suliko.ge about a running job and act on the answer. Returns its
    progress while it still runs.

    suliko.ge being unreachable is not news about the job: it is left as it
    is, to be asked about on the next look.
    """
    if row.status is not TranslationStatus.PROCESSING:
        return None
    try:
        status = await translator.status(row.suliko_job_id)
        if status.state == "completed":
            await _collect(db, storage, translator, row, order, document)
            return None
    except SulikoUnavailableError:
        return None
    if status.state == "failed":
        # suliko.ge refunds the pages of a job that fails. Its own message can
        # name internals, so it is logged and a plain one is kept.
        log.warning("suliko_translation_failed", job=row.suliko_job_id, message=status.message)
        row.status = TranslationStatus.FAILED
        row.error = "The translation did not finish. No pages were charged."
        row.finished_at = datetime.now(UTC)
        await db.flush()
        return None
    return status.progress


@router.get(
    "/{order_id}/documents/{document_id}/translations",
    response_model=list[TranslationOut],
)
async def list_translations(
    order_id: int,
    document_id: int,
    db: Db,
    storage: Storage,
    translator: Translator,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> list[TranslationOut]:
    """The document's translations, newest first. Looking is what moves a
    running one along: see the module's note on there being no worker."""
    order, document = await _document(db, order_id, document_id)
    rows = list(
        (
            await db.execute(
                select(DocumentTranslation)
                .where(DocumentTranslation.order_document_id == document_id)
                .order_by(DocumentTranslation.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    progress = {
        row.id: await refresh_translation(db, storage, translator, row, order, document)
        for row in rows
    }
    files = await public_file_ids(db, rows)
    return [translation_out(row, files, progress=progress[row.id]) for row in rows]


# ── Correcting one ──────────────────────────────────────────────────────────


async def _translations_of(db: AsyncSession, file: OrderFile) -> list[DocumentTranslation]:
    """The translation records whose result is this file (one, in practice)."""
    return list(
        (
            await db.execute(
                select(DocumentTranslation).where(DocumentTranslation.result_file_id == file.id)
            )
        )
        .scalars()
        .all()
    )


@router.get(
    "/{order_id}/documents/{document_id}/files/{file_id}/html",
    response_model=TranslationHtmlOut,
)
async def get_translation_html(
    order_id: int,
    document_id: int,
    file_id: FileIdPath,
    db: Db,
    storage: Storage,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> TranslationHtmlOut:
    """A translation as the page that corrects it edits it.

    Kept on the translation's record for a Word file; read from the file for
    one filed as HTML. Any other file is not for editing here.
    """
    await _document(db, order_id, document_id)
    current = await get_document_file(db, document_id, file_id)
    if current.kind is not FileKind.TRANSLATION:
        raise NotFoundError("This file cannot be edited here.")
    for record in await _translations_of(db, current):
        if record.html is not None:
            return TranslationHtmlOut(html=record.html)
    if (
        current.content_type != HTML
        or current.size_bytes > get_settings().translation_html_max_bytes
    ):
        raise NotFoundError("This file cannot be edited here.")
    try:
        body = await open_download(storage, current)
        content = b"".join([chunk async for chunk in body])
    except StorageError as exc:
        raise storage_failure(exc) from exc
    return TranslationHtmlOut(html=content.decode("utf-8-sig", errors="replace"))


@router.put(
    "/{order_id}/documents/{document_id}/files/{file_id}/content",
    response_model=StaffFileOut,
)
async def save_translation(
    order_id: int,
    document_id: int,
    file_id: FileIdPath,
    payload: SaveContentIn,
    db: Db,
    session: CurrentSession,
    storage: Storage,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> StaffFileOut:
    """Save a corrected translation: a new file in the old one's place.

    The Word file is built again from the corrected HTML, so a translation
    filed as HTML before there were Word files becomes one on its first save.
    Only a translation made here can be saved this way: it is the one kind of
    file Office shows for editing. The file it replaces is removed like any
    other, so it can be restored for the retention period.
    """
    order, document = await _document(db, order_id, document_id)
    current = await get_document_file(db, document_id, file_id)
    records = await _translations_of(db, current)
    editable = current.content_type == HTML or any(record.html is not None for record in records)
    if current.kind is not FileKind.TRANSLATION or not editable:
        raise ConflictError("Only a translation made here can be edited here.")
    if len(payload.html.encode("utf-8")) > get_settings().translation_html_max_bytes:
        raise PayloadTooLargeError("This translation is too large to save.")

    # An HTML file with no record behind it has nowhere to keep the HTML a Word
    # file needs, so it stays HTML as it was.
    made = await make_translation_file(
        payload.html,
        stem=PurePosixPath(current.file_name).stem or "translation",
        language=records[0].target_language if records else document.target_language,
        word=bool(records),
    )
    try:
        stored = await upload_document_file(
            db,
            storage,
            order=order,
            document=document,
            kind=FileKind.TRANSLATION,
            file_name=made.name,
            content=made.content,
            content_type=made.content_type,
            uploaded_by=f"user:{session.user_id}",
        )
    except StorageError as exc:
        raise storage_failure(exc) from exc
    await remove_document_file(db, current, removed_by=f"user:{session.user_id}")
    # The translation's record follows its file.
    for record in records:
        record.result_file_id = stored.id
        record.html = made.html
    await db.flush()

    from suliko.core.audit import record as audit

    await audit(
        db,
        session,
        action="order.translation_edited",
        entity_type="order",
        entity_id=order_id,
        after={"document_id": document_id, "file_name": stored.file_name},
    )
    return _out(stored, editable=made.html is not None or made.content_type == HTML)
