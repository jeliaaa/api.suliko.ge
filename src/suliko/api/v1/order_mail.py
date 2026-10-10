"""Emailing an order's files: to its client, or to a translator working on it.

Two routes. `GET /orders/{id}/mail-options` says who can be written to and how
the message would leave, so the form can show the address before anyone
presses Send. `POST /orders/{id}/send-files` sends.

## Who can be written to

Only the addresses already on record: the order's client, and translators
assigned to its documents. The address is never taken from the request, so
this cannot be turned into a way to mail the bureau's files (or anything
else) to an address of the caller's choosing, and a typo in a form cannot
send a passport scan to a stranger. A missing address is fixed on the
client's or translator's own page.

A translator is sent only files of documents assigned to them. That is the
same line the translator portal draws.

## How it leaves

`core.business_mail.resolve_sender`: the bureau's own SMTP server if it has
set one up, otherwise the platform's, under the bureau's name. Sends are
counted per bureau (`document_mail_max_per_tenant`), since the second path
is our address and our reputation.
"""

from __future__ import annotations

from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import CurrentSession, Db, require
from suliko.api.portal_deps import Storage
from suliko.api.v1.order_files import storage_failure
from suliko.config import get_settings
from suliko.core.business_mail import (
    Attachment,
    MailNotConfiguredError,
    Sender,
    Via,
    build_message,
    deliver,
    is_address,
    resolve_sender,
)
from suliko.core.errors import (
    NotFoundError,
    PayloadTooLargeError,
    RateLimitedError,
    ValidationError,
)
from suliko.core.ratelimit import RateLimiter, get_rate_limiter
from suliko.domain.order_files import FILE_ID_PATTERN, downloadable, open_download
from suliko.integrations.object_storage import StorageError
from suliko.models.directory import Client, Translator
from suliko.models.order import Order, OrderDocument
from suliko.models.order_file import OrderFile
from suliko.models.reference import TenantSettings
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

log = structlog.get_logger()

router = APIRouter(prefix="/orders", tags=["order-mail"])

Language = Literal["ka", "en"]
MAX_FILES = 20


class Recipient(BaseModel):
    name: str
    #: None when there is no address on record: nothing can be sent yet.
    email: str | None


class TranslatorRecipient(Recipient):
    id: int
    #: The documents of this order assigned to them. Only those documents'
    #: files may be sent to this translator.
    document_ids: list[int]


class MailOptions(BaseModel):
    client: Recipient
    #: Empty without `translators.read`, and on an order nobody is assigned to.
    translators: list[TranslatorRecipient]
    #: "smtp": the bureau's own server. "platform": Suliko's address, under
    #: the bureau's name. None: no mail server is available, sending is off.
    via: Via | None
    from_email: str | None
    #: Where a reply goes when the message leaves from Suliko's address.
    reply_to: str | None
    #: The most the attached files may weigh together.
    max_bytes: int


class SendFilesIn(BaseModel):
    to: Literal["client", "translator"]
    #: Required with `to: "translator"`.
    translator_id: int | None = None
    file_ids: list[str] = Field(min_length=1, max_length=MAX_FILES)
    #: The sender's own words, placed above the list of files.
    message: str | None = Field(default=None, max_length=2000)
    #: The language of the fixed sentences. Defaults to the bureau's own.
    language: Language | None = None


class SendFilesOut(BaseModel):
    recipient: str
    files: int
    via: Via


def _valid(email: str | None) -> str | None:
    return email.strip() if email and is_address(email) else None


async def _order(db: AsyncSession, order_id: int) -> Order:
    order = await db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found.")
    return order


async def _assigned(db: AsyncSession, order_id: int) -> dict[int, list[int]]:
    """Translator id -> the documents of this order assigned to them."""
    rows = (
        await db.execute(
            select(OrderDocument.id, OrderDocument.translator_id)
            .where(OrderDocument.order_id == order_id, OrderDocument.translator_id.is_not(None))
            .order_by(OrderDocument.id)
        )
    ).all()
    assigned: dict[int, list[int]] = {}
    for document_id, translator_id in rows:
        assigned.setdefault(translator_id, []).append(document_id)
    return assigned


async def _sender(db: AsyncSession, session: AuthenticatedSession) -> Sender | None:
    settings_row = (await db.execute(select(TenantSettings))).scalars().first()
    # The bureau's own address if it has set one; failing that, the person
    # sending, so a reply still reaches someone who knows the order.
    reply_to = (settings_row.system_email if settings_row else None) or session.email
    return await resolve_sender(
        db, session.tenant_id, organisation=session.tenant_name, reply_to=reply_to
    )


@router.get("/{order_id}/mail-options", response_model=MailOptions)
async def mail_options(
    order_id: int,
    db: Db,
    session: CurrentSession,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> MailOptions:
    order = await _order(db, order_id)
    client = await db.get(Client, order.client_id)

    translators: list[TranslatorRecipient] = []
    if session.has(Permission.TRANSLATORS_READ):
        assigned = await _assigned(db, order_id)
        if assigned:
            people = (
                (
                    await db.execute(
                        select(Translator)
                        .where(Translator.id.in_(assigned))
                        .order_by(Translator.name)
                    )
                )
                .scalars()
                .all()
            )
            translators = [
                TranslatorRecipient(
                    id=person.id,
                    name=person.name,
                    email=_valid(person.email),
                    document_ids=assigned[person.id],
                )
                for person in people
            ]

    sender = await _sender(db, session)
    return MailOptions(
        client=Recipient(
            name=client.name if client else "", email=_valid(client.email) if client else None
        ),
        translators=translators,
        via=sender.via if sender else None,
        from_email=sender.from_email if sender else None,
        reply_to=sender.reply_to if sender else None,
        max_bytes=get_settings().document_mail_max_bytes,
    )


# ── The words ───────────────────────────────────────────────────────────────

_SUBJECT: dict[Language, str] = {
    "en": "Documents for order #{number} from {organisation}",
    "ka": "დოკუმენტები შეკვეთისთვის №{number}: {organisation}",
}
_GREETING: dict[Language, str] = {"en": "Hello {name},", "ka": "გამარჯობა, {name}!"}
_INTRO: dict[tuple[str, Language], str] = {
    ("client", "en"): "{organisation} has sent you these files for order #{number}:",
    ("client", "ka"): "{organisation} გიგზავნით ამ ფაილებს შეკვეთისთვის №{number}:",
    ("translator", "en"): "{organisation} has sent you these files to work on, order #{number}:",
    ("translator", "ka"): "{organisation} გიგზავნით ამ ფაილებს სამუშაოდ, შეკვეთა №{number}:",
}
_REPLY: dict[Language, str] = {
    "en": "To get in touch, reply to this email.",
    "ka": "დასაკავშირებლად უპასუხეთ ამ წერილს.",
}


def compose(
    *,
    to: Literal["client", "translator"],
    language: Language,
    organisation: str,
    order_number: int,
    recipient_name: str,
    file_names: list[str],
    note: str | None,
) -> tuple[str, str]:
    """Subject and plain-text body. The note is the sender's own and goes in
    as written; everything around it is fixed."""
    subject = _SUBJECT[language].format(number=order_number, organisation=organisation)
    parts = [
        _GREETING[language].format(name=recipient_name),
        _INTRO[(to, language)].format(organisation=organisation, number=order_number)
        + "\n"
        + "\n".join(f"- {name}" for name in file_names),
    ]
    if note and note.strip():
        parts.append(note.strip())
    parts.append(_REPLY[language])
    return subject, "\n\n".join(parts) + "\n"


# ── Sending ─────────────────────────────────────────────────────────────────


async def _files(db: AsyncSession, order_id: int, file_ids: list[str]) -> list[OrderFile]:
    """The named files, each a live file of a document of THIS order, in the
    order they were named. Anything else is a 404, never a partial send."""
    wanted = list(dict.fromkeys(file_ids))
    if not all(FILE_ID_PATTERN.fullmatch(file_id) for file_id in wanted):
        raise NotFoundError("File not found.")
    rows = (
        (
            await db.execute(
                select(OrderFile)
                .join(OrderDocument, OrderDocument.id == OrderFile.order_document_id)
                .where(
                    OrderFile.public_id.in_(wanted),
                    OrderFile.deleted_at.is_(None),
                    OrderDocument.order_id == order_id,
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {row.public_id: row for row in rows}
    if len(by_id) != len(wanted):
        raise NotFoundError("File not found.")
    return [by_id[file_id] for file_id in wanted]


@router.post("/{order_id}/send-files", response_model=SendFilesOut)
async def send_files(
    order_id: int,
    payload: SendFilesIn,
    db: Db,
    session: CurrentSession,
    storage: Storage,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> SendFilesOut:
    order = await _order(db, order_id)
    rows = await _files(db, order_id, payload.file_ids)

    if payload.to == "client":
        client = await db.get(Client, order.client_id)
        name = client.name if client else ""
        email = _valid(client.email) if client else None
        if email is None:
            raise ValidationError(
                "This client has no email address. Add one on the client's page first."
            )
    else:
        assigned = await _assigned(db, order_id)
        translator_id = payload.translator_id
        if translator_id is None or translator_id not in assigned:
            raise ValidationError("That translator is not assigned to this order.")
        translator = await db.get(Translator, translator_id)
        if translator is None:
            raise ValidationError("That translator is not assigned to this order.")
        name = translator.name
        email = _valid(translator.email)
        if email is None:
            raise ValidationError(
                "This translator has no email address. Add one on the translator's page first."
            )
        theirs = set(assigned[translator_id])
        if any(row.order_document_id not in theirs for row in rows):
            raise ValidationError(
                "Only files of the documents assigned to this translator can be sent to them."
            )

    settings = get_settings()
    if any(not downloadable(row) for row in rows):
        raise ValidationError(
            "One of these files is kept in the Order Vault and cannot be sent from here."
        )
    total = sum(row.size_bytes for row in rows)
    if total > settings.document_mail_max_bytes:
        limit_mb = settings.document_mail_max_bytes // (1024 * 1024)
        raise PayloadTooLargeError(
            f"These files are larger than an email can carry ({limit_mb} MB together). "
            "Send fewer at a time."
        )

    sender = await _sender(db, session)
    if sender is None:
        raise MailNotConfiguredError("Email is not set up on the server yet.")

    limit_key = f"docmail:tenant:{session.tenant_id}"
    if retry := await limiter.check_document_mail(limit_key):
        raise RateLimitedError(
            "This organisation has sent a lot of email today. Try again later.",
            retry_after=retry,
        )

    attachments: list[Attachment] = []
    for row in rows:
        try:
            body = await open_download(storage, row)
            content = b"".join([chunk async for chunk in body])
        except StorageError as exc:
            raise storage_failure(exc) from exc
        attachments.append(
            Attachment(file_name=row.file_name, content_type=row.content_type, content=content)
        )

    settings_row = (await db.execute(select(TenantSettings))).scalars().first()
    language: Language = payload.language or (
        "en" if settings_row is not None and settings_row.default_language == "en" else "ka"
    )
    subject, text = compose(
        to=payload.to,
        language=language,
        organisation=session.tenant_name,
        order_number=order.number,
        recipient_name=name,
        file_names=[row.file_name for row in rows],
        note=payload.message,
    )
    await deliver(
        sender,
        build_message(sender, to=email, subject=subject, body=text, attachments=attachments),
    )
    await limiter.record_document_mail(limit_key)

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.files_sent",
        entity_type="order",
        entity_id=order_id,
        after={
            "to": payload.to,
            "recipient": email,
            "via": sender.via,
            "files": [row.file_name for row in rows],
        },
    )
    return SendFilesOut(recipient=email, files=len(rows), via=sender.via)
