"""Emailing an order's files to its client or a translator.

What matters: the message goes only to an address already on record, carries
exactly the files asked for and only files this order (and, for a translator,
their own documents) owns, leaves through the right server under the right
name, and says so plainly when it cannot.

The handlers are called directly with a staff stand-in, as in
``test_order_files_api.py``; storage is a real ``LocalDiskStorage``; the SMTP
conversation is replaced by a recorder.
"""

from __future__ import annotations

import io
import json
import smtplib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from suliko.api.v1 import order_files, order_mail
from suliko.api.v1.order_mail import SendFilesIn
from suliko.config import Settings
from suliko.core import business_mail
from suliko.core.business_mail import MailDeliveryError, MailNotConfiguredError, Sender
from suliko.core.crypto import encrypt_for_tenant
from suliko.core.errors import (
    NotFoundError,
    PayloadTooLargeError,
    RateLimitedError,
    ValidationError,
)
from suliko.core.ratelimit import RateLimiter
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.integrations.object_storage import LocalDiskStorage
from suliko.models.directory import Client, ClientType, Translator
from suliko.models.integration import IntegrationCredential, IntegrationProvider
from suliko.models.order import CopyType, Order, OrderDocument, OrderStatusEvent, Urgency
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind
from suliko.models.reference import DocumentType, Language, TenantSettings
from suliko.models.tenant import Tenant, TenantStatus
from suliko.security.permissions import Permission

ACME, GLOBEX = 1, 2
NINO, LEVAN = 11, 12  # translators at ACME


@dataclass
class Staff:
    user_id: int = 7
    tenant_id: int = ACME
    tenant_name: str = "Acme Translations"
    email: str | None = "owner@acme.ge"
    permissions: frozenset[Permission] = field(
        default_factory=lambda: frozenset({Permission.ORDERS_WRITE, Permission.TRANSLATORS_READ})
    )

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions


PLATFORM = Sender(
    via="platform",
    host="relay.suliko.ge",
    port=587,
    username=None,
    password=None,
    starttls=True,
    ssl=False,
    from_email="noreply@suliko.ge",
    from_name="Acme Translations via Suliko",
    reply_to="office@acme.ge",
    timeout=20,
)


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    async def record(_db: object, _session: object, **kwargs: Any) -> None:
        entries.append(kwargs)

    monkeypatch.setattr("suliko.core.audit.record", record)
    return entries


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Sender, EmailMessage]]:
    """Every message handed to an SMTP server, and through which sender."""
    sent: list[tuple[Sender, EmailMessage]] = []
    monkeypatch.setattr(
        business_mail, "_send_blocking", lambda sender, message: sent.append((sender, message))
    )
    return sent


@pytest.fixture
def sender(monkeypatch: pytest.MonkeyPatch) -> dict[str, Sender | None]:
    """What `resolve_sender` answers. The choice itself is tested on its own
    below; the integrations table is JSONB and does not exist on SQLite."""
    box: dict[str, Sender | None] = {"sender": PLATFORM}

    async def resolve(_db: object, _tenant_id: int, **_: Any) -> Sender | None:
        return box["sender"]

    monkeypatch.setattr(order_mail, "resolve_sender", resolve)
    return box


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    tables = [
        m.__table__
        for m in (
            Tenant,
            Client,
            Translator,
            DocumentType,
            Language,
            TenantSettings,
            Order,
            OrderDocument,
            OrderFile,
            OrderStatusEvent,
        )
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as session:
        with bypass_tenant_scope():
            for tenant_id, slug in ((ACME, "acme"), (GLOBEX, "globex")):
                session.add(
                    Tenant(
                        id=tenant_id,
                        slug=slug,
                        display_name=slug,
                        status=TenantStatus.ACTIVE,
                        locale="ka",
                    )
                )
            await session.flush()
            for tenant_id in (ACME, GLOBEX):
                session.add_all(
                    [
                        Client(
                            id=tenant_id,
                            tenant_id=tenant_id,
                            name="Giorgi Client",
                            client_type=ClientType.B2C,
                            email=f"client@{'acme' if tenant_id == ACME else 'globex'}.test",
                        ),
                        DocumentType(
                            id=tenant_id,
                            tenant_id=tenant_id,
                            name_en="Passport",
                            name_ka="პასპორტი",
                        ),
                    ]
                )
            session.add_all(
                [
                    Language(
                        id=1, tenant_id=ACME, code="ka", name_en="Georgian", name_ka="ქართული"
                    ),
                    Language(
                        id=2, tenant_id=ACME, code="en", name_en="English", name_ka="ინგლისური"
                    ),
                    Translator(id=NINO, tenant_id=ACME, name="Nino", email="nino@example.test"),
                    Translator(id=LEVAN, tenant_id=ACME, name="Levan", email=None),
                    TenantSettings(tenant_id=ACME, default_language="ka", system_email=None),
                ]
            )
            await session.flush()
            for tenant_id in (ACME, GLOBEX):
                session.add(
                    Order(
                        id=tenant_id * 100,
                        tenant_id=tenant_id,
                        client_id=tenant_id,
                        order_date=date(2026, 10, 1),
                        due_date=date(2026, 10, 25),
                        urgency=Urgency.EXPRESS if tenant_id == ACME else Urgency.STANDARD,
                        notes="internal: client is slow to pay",
                    )
                )
            await session.flush()
            # ACME's order has two documents: 1000 is Nino's, 1001 is Levan's.
            for document_id, tenant_id, translator_id in (
                (1000, ACME, NINO),
                (1001, ACME, LEVAN),
                (2000, GLOBEX, None),
            ):
                session.add(
                    OrderDocument(
                        id=document_id,
                        tenant_id=tenant_id,
                        order_id=tenant_id * 100,
                        document_type_id=tenant_id,
                        source_language="ka",
                        target_language="en",
                        page_count=1,
                        copy_type=CopyType.ORIGINAL,
                        price=Decimal("10"),
                        translator_cost=Decimal("0"),
                        notary_cost=Decimal("0"),
                        translator_id=translator_id,
                    )
                )
            await session.commit()
        yield session
    await engine.dispose()


async def _put(
    db: AsyncSession,
    storage: LocalDiskStorage,
    document_id: int,
    name: str,
    content: bytes,
    *,
    tenant_id: int = ACME,
    kind: FileKind = FileKind.TRANSLATION,
) -> str:
    upload = UploadFile(
        file=io.BytesIO(content),
        filename=name,
        headers=Headers({"content-type": "application/pdf"}),
    )
    out = await order_files.upload_file(
        tenant_id * 100,
        document_id,
        upload,
        db,
        Staff(tenant_id=tenant_id),
        storage,
        None,  # type: ignore[arg-type]
        kind=kind,
    )
    return out.id


async def _send(
    db: AsyncSession,
    storage: LocalDiskStorage,
    payload: SendFilesIn,
    *,
    staff: Staff | None = None,
    order_id: int = 100,
    limiter: RateLimiter | None = None,
) -> order_mail.SendFilesOut:
    return await order_mail.send_files(
        order_id,
        payload,
        db,
        staff or Staff(),  # type: ignore[arg-type]
        storage,
        limiter or RateLimiter(),
        None,  # type: ignore[arg-type]
    )


def _attachments(message: EmailMessage) -> dict[str, bytes]:
    return {
        part.get_filename() or "": part.get_payload(decode=True)  # type: ignore[misc]
        for part in message.iter_attachments()
    }


# ── To the client ───────────────────────────────────────────────────────────


async def test_the_client_gets_the_files_from_the_bureau_by_name(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
    audit: list[dict[str, Any]],
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        first = await _put(db, storage, 1000, "passport-en.pdf", b"%PDF one")
        second = await _put(db, storage, 1001, "diploma-en.pdf", b"%PDF two")
        audit.clear()

        out = await _send(
            db,
            storage,
            SendFilesIn(to="client", file_ids=[first, second], message="  Ready, thank you.  "),
        )

    assert (out.recipient, out.files, out.via) == ("client@acme.test", 2, "platform")
    [(used, message)] = outbox
    # Suliko's server, with the display name changed to name the order.
    assert replace(used, from_name=PLATFORM.from_name) == PLATFORM
    assert message["To"] == "client@acme.test"
    # The bureau and which order, in place of "via Suliko".
    assert message["From"] == '"Acme Translations, შეკვეთა №1" <noreply@suliko.ge>'
    assert message["Reply-To"] == "office@acme.ge"
    assert _attachments(message) == {"passport-en.pdf": b"%PDF one", "diploma-en.pdf": b"%PDF two"}

    body = message.get_body(preferencelist=("plain",)).get_content()  # type: ignore[union-attr]
    # The bureau's default language, the files by name, and the note as written.
    assert "გამარჯობა, Giorgi Client!" in body
    # The order's facts, then each document with its files under it.
    assert "ვადა: 25.10.2026\nსასწრაფოობა: ექსპრესი" in body
    kind = "პასპორტი (ქართული → ინგლისური, 1 გვერდი, ორიგინალი დოკუმენტი)"
    assert f"{kind}\n- passport-en.pdf\n\n{kind}\n- diploma-en.pdf" in body
    assert "\n\nReady, thank you.\n\n" in body

    [entry] = audit
    assert entry["action"] == "order.files_sent"
    assert entry["after"] == {
        "to": "client",
        "recipient": "client@acme.test",
        "via": "platform",
        "files": ["passport-en.pdf", "diploma-en.pdf"],
    }


async def test_the_sender_can_ask_for_english(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _put(db, storage, 1000, "a.pdf", b"x")
        await _send(db, storage, SendFilesIn(to="client", file_ids=[file_id], language="en"))

    [(_, message)] = outbox
    assert message["Subject"] == "Documents for order #1 from Acme Translations"
    body = message.get_body(preferencelist=("plain",)).get_content()  # type: ignore[union-attr]
    assert body.startswith("Hello Giorgi Client,\n\n")
    assert "To get in touch, reply to this email." in body
    assert "Due date: 25.10.2026\nUrgency: Express" in body
    assert "Passport (Georgian → English, 1 page, Original document)\n- a.pdf" in body
    assert message["From"] == '"Acme Translations, order #1" <noreply@suliko.ge>'


async def test_the_email_also_carries_a_plain_html_version_with_everything_escaped(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _put(db, storage, 1000, "a&b.pdf", b"%PDF")
        client = await db.get(Client, ACME)
        assert client is not None
        client.name = "Giorgi <Client>"
        await db.flush()
        await _send(
            db,
            storage,
            SendFilesIn(
                to="client",
                file_ids=[file_id],
                language="en",
                message='Please <b>check</b> it & "sign"\nThank you',
            ),
        )

    [(_, message)] = outbox
    html_part = message.get_body(preferencelist=("html",))
    assert html_part is not None
    page = html_part.get_content()
    # Words that came from a person are escaped, the sender's line breaks kept.
    assert "Hello Giorgi &lt;Client&gt;," in page
    assert "a&amp;b.pdf" in page
    assert "Please &lt;b&gt;check&lt;/b&gt; it &amp; &quot;sign&quot;<br>Thank you" in page
    assert "<strong>Acme Translations</strong>" in page
    assert "Sent through Suliko." in page
    # Nothing that makes a message look like a campaign or a tracker.
    for marker in ("<img", "href=", "<a ", "<script", "<style", "<link"):
        assert marker not in page
    # The plain text is still there, and so are the files.
    plain = message.get_body(preferencelist=("plain",))
    assert plain is not None
    assert plain.get_content().startswith("Hello Giorgi <Client>,\n\n")
    assert _attachments(message) == {"a&b.pdf": b"%PDF"}


async def test_the_html_version_can_be_switched_off(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(order_mail, "get_settings", lambda: Settings(document_mail_html=False))
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _put(db, storage, 1000, "a.pdf", b"x")
        await _send(db, storage, SendFilesIn(to="client", file_ids=[file_id], language="en"))

    [(_, message)] = outbox
    assert message.get_body(preferencelist=("html",)) is None
    assert message.get_body(preferencelist=("plain",)) is not None
    assert _attachments(message) == {"a.pdf": b"x"}


async def test_the_html_shows_the_order_card_and_each_document_with_its_files(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        first = await _put(db, storage, 1000, "passport-en.pdf", b"%PDF one")
        second = await _put(db, storage, 1000, "stamp.docx", b"two")
        await _send(db, storage, SendFilesIn(to="client", file_ids=[first, second], language="en"))

    [(_, message)] = outbox
    html_part = message.get_body(preferencelist=("html",))
    assert html_part is not None
    page = html_part.get_content()
    # The order card: number, due date, and the urgency as a tag.
    assert "#1</div>" in page
    assert "25.10.2026" in page
    assert ">Express</span>" in page
    # One document, with its type, languages, pages and kind of copy, and both
    # files under it, each tagged with its type.
    assert page.count("Passport</div>") == 1
    assert "Georgian → English &middot; 1 page &middot; Original document" in page
    assert ">PDF</span>passport-en.pdf" in page
    assert ">DOCX</span>stamp.docx" in page
    # Suliko's blue is the only colour added.
    assert "#3b59f3" in page


async def test_a_translator_is_told_the_work_and_never_money_notes_or_the_kind_of_copy(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _put(db, storage, 1000, "scan.pdf", b"source", kind=FileKind.SOURCE)
        await _send(
            db,
            storage,
            SendFilesIn(to="translator", translator_id=NINO, file_ids=[file_id], language="en"),
        )

    [(_, message)] = outbox
    plain = message.get_body(preferencelist=("plain",))
    html_part = message.get_body(preferencelist=("html",))
    assert plain is not None and html_part is not None
    for body in (plain.get_content(), html_part.get_content()):
        # What the work is, and when.
        assert "Georgian → English" in body
        assert "25.10.2026" in body
        assert "Express" in body
        # What they have no use for, or must not see: the kind of copy, the
        # price, the bureau's internal note, the client's details.
        assert "Original document" not in body
        assert "GEL" not in body and "10.00" not in body
        assert "slow to pay" not in body
        assert "client@acme.test" not in body and "Giorgi" not in body


async def test_the_bureaus_internal_note_never_reaches_a_client_either(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _put(db, storage, 1000, "a.pdf", b"x")
        await _send(db, storage, SendFilesIn(to="client", file_ids=[file_id]))

    [(_, message)] = outbox
    for part in message.iter_parts():
        if part.get_content_type() in ("text/plain", "text/html"):
            assert "slow to pay" not in part.get_content()
    assert "slow to pay" not in message.as_string()


async def test_a_bureaus_own_server_keeps_the_name_it_was_set_up_with(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
) -> None:
    sender["sender"] = replace(PLATFORM, via="smtp", from_email="office@acme.ge", from_name="Acme")
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _put(db, storage, 1000, "a.pdf", b"x")
        await _send(db, storage, SendFilesIn(to="client", file_ids=[file_id], language="en"))

    [(_, message)] = outbox
    assert message["From"] == "Acme <office@acme.ge>"


async def test_a_language_the_bureau_has_not_named_shows_as_its_code(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(GLOBEX):
        file_id = await _put(db, storage, 2000, "g.pdf", b"x", tenant_id=GLOBEX)
        await _send(
            db,
            storage,
            SendFilesIn(to="client", file_ids=[file_id], language="en"),
            staff=Staff(tenant_id=GLOBEX, tenant_name="Globex"),
            order_id=200,
        )

    [(_, message)] = outbox
    plain = message.get_body(preferencelist=("plain",))
    assert plain is not None
    assert "Passport (KA → EN, 1 page, Original document)" in plain.get_content()


async def test_a_client_without_an_address_is_said_so_and_nothing_is_sent(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _put(db, storage, 1000, "a.pdf", b"x")
        client = await db.get(Client, ACME)
        assert client is not None
        client.email = "not an address"
        await db.flush()

        with pytest.raises(ValidationError, match="no email address"):
            await _send(db, storage, SendFilesIn(to="client", file_ids=[file_id]))
    assert outbox == []


# ── Only this order's files ─────────────────────────────────────────────────


async def test_a_file_of_another_bureau_or_a_removed_one_sends_nothing(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(GLOBEX):
        theirs = await _put(db, storage, 2000, "globex.pdf", b"secret", tenant_id=GLOBEX)
    with tenant_scope(ACME):
        ours = await _put(db, storage, 1000, "ours.pdf", b"x")
        gone = await _put(db, storage, 1000, "gone.pdf", b"y")
        await order_files.delete_file(100, 1000, gone, db, Staff(), None)  # type: ignore[arg-type]

        for file_ids in ([ours, theirs], [gone], ["no-such-file-id"], ["../../etc"]):
            with pytest.raises(NotFoundError):
                await _send(db, storage, SendFilesIn(to="client", file_ids=file_ids))
    # One bad id fails the whole send: never a message with some of the files.
    assert outbox == []


async def test_files_too_heavy_for_an_email_are_refused(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(order_mail, "get_settings", lambda: Settings(document_mail_max_bytes=10))
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _put(db, storage, 1000, "big.pdf", b"x" * 11)
        with pytest.raises(PayloadTooLargeError):
            await _send(db, storage, SendFilesIn(to="client", file_ids=[file_id]))
    assert outbox == []


# ── To a translator ─────────────────────────────────────────────────────────


async def test_a_translator_gets_only_their_own_documents_files(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        ninos = await _put(db, storage, 1000, "scan.pdf", b"source", kind=FileKind.SOURCE)
        levans = await _put(db, storage, 1001, "other.pdf", b"other", kind=FileKind.SOURCE)

        out = await _send(
            db, storage, SendFilesIn(to="translator", translator_id=NINO, file_ids=[ninos])
        )
        assert out.recipient == "nino@example.test"

        with pytest.raises(ValidationError, match="assigned to this translator"):
            await _send(
                db,
                storage,
                SendFilesIn(to="translator", translator_id=NINO, file_ids=[ninos, levans]),
            )
        # Levan is assigned, but has no address on record.
        with pytest.raises(ValidationError, match="no email address"):
            await _send(
                db, storage, SendFilesIn(to="translator", translator_id=LEVAN, file_ids=[levans])
            )
        # Someone who is not on this order at all, or nobody named.
        for translator_id in (999, None):
            with pytest.raises(ValidationError, match="not assigned"):
                await _send(
                    db,
                    storage,
                    SendFilesIn(to="translator", translator_id=translator_id, file_ids=[ninos]),
                )

    [(_, message)] = outbox
    assert message["To"] == "nino@example.test"
    assert _attachments(message) == {"scan.pdf": b"source"}
    body = message.get_body(preferencelist=("plain",)).get_content()  # type: ignore[union-attr]
    assert "სამუშაოდ" in body


# ── When it cannot leave ────────────────────────────────────────────────────


async def test_no_mail_server_at_all_is_a_clear_refusal(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
) -> None:
    sender["sender"] = None
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _put(db, storage, 1000, "a.pdf", b"x")
        with pytest.raises(MailNotConfiguredError):
            await _send(db, storage, SendFilesIn(to="client", file_ids=[file_id]))
    assert outbox == []


async def test_a_refused_message_is_told_and_not_counted_or_audited(
    db: AsyncSession,
    tmp_path: Path,
    sender: dict[str, Sender | None],
    audit: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_sender: Sender, _message: EmailMessage) -> None:
        raise smtplib.SMTPRecipientsRefused({"client@acme.test": (550, b"no such user")})

    monkeypatch.setattr(business_mail, "_send_blocking", refuse)
    limiter = RateLimiter()
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _put(db, storage, 1000, "a.pdf", b"x")
        audit.clear()
        with pytest.raises(MailDeliveryError) as raised:
            await _send(db, storage, SendFilesIn(to="client", file_ids=[file_id]), limiter=limiter)

    # The server's own words stay in the log.
    assert "no such user" not in str(raised.value)
    assert audit == []
    assert await limiter.check_document_mail(f"docmail:tenant:{ACME}") is None


async def test_a_bureau_that_sends_too_much_is_stopped(
    db: AsyncSession,
    tmp_path: Path,
    outbox: list[tuple[Sender, EmailMessage]],
    sender: dict[str, Sender | None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limited = Settings(document_mail_max_per_tenant=1)
    monkeypatch.setattr("suliko.core.ratelimit.get_settings", lambda: limited)
    limiter = RateLimiter()
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _put(db, storage, 1000, "a.pdf", b"x")
        payload = SendFilesIn(to="client", file_ids=[file_id])
        await _send(db, storage, payload, limiter=limiter)
        with pytest.raises(RateLimitedError):
            await _send(db, storage, payload, limiter=limiter)
    assert len(outbox) == 1


# ── What the form is told ───────────────────────────────────────────────────


async def test_the_options_name_who_can_be_written_to_and_how(
    db: AsyncSession, sender: dict[str, Sender | None]
) -> None:
    with tenant_scope(ACME):
        options = await order_mail.mail_options(100, db, Staff(), None)  # type: ignore[arg-type]
        assert options.client.email == "client@acme.test"
        assert [(t.name, t.email, t.document_ids) for t in options.translators] == [
            ("Levan", None, [1001]),
            ("Nino", "nino@example.test", [1000]),
        ]
        assert (options.via, options.from_email, options.reply_to) == (
            "platform",
            "noreply@suliko.ge",
            "office@acme.ge",
        )

        # Without the right to see translators, none are named.
        plain = Staff(permissions=frozenset({Permission.ORDERS_WRITE}))
        assert (await order_mail.mail_options(100, db, plain, None)).translators == []  # type: ignore[arg-type]

        sender["sender"] = None
        assert (await order_mail.mail_options(100, db, Staff(), None)).via is None  # type: ignore[arg-type]

    with tenant_scope(GLOBEX), pytest.raises(NotFoundError):
        await order_mail.mail_options(100, db, Staff(tenant_id=GLOBEX), None)  # type: ignore[arg-type]


# ── Which server ────────────────────────────────────────────────────────────


class _OneRow:
    """Stands in for the session: answers the one query `resolve_sender` makes."""

    def __init__(self, row: IntegrationCredential | None) -> None:
        self._row = row

    async def execute(self, _statement: object) -> _OneRow:
        return self

    def scalar_one_or_none(self) -> IntegrationCredential | None:
        return self._row


def _smtp_row(**config: Any) -> IntegrationCredential:
    base: dict[str, Any] = {
        "host": "mail.acme.ge",
        "port": "465",
        "username": "office",
        "from_email": "office@acme.ge",
        "use_tls": False,
    }
    base.update(config)
    return IntegrationCredential(
        tenant_id=ACME,
        provider=IntegrationProvider.SMTP,
        is_enabled=True,
        config=base,
        secrets=encrypt_for_tenant(ACME, json.dumps({"password": "s3cret"})),
    )


def _with_platform(monkeypatch: pytest.MonkeyPatch, configured: bool = True) -> None:
    settings = (
        Settings(smtp_host="relay.suliko.ge", smtp_from_email="noreply@suliko.ge")
        if configured
        else Settings(smtp_host=None, smtp_from_email=None)
    )
    monkeypatch.setattr(business_mail, "get_settings", lambda: settings)


async def test_a_bureaus_own_server_is_used_when_it_is_set_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_platform(monkeypatch)
    own = await business_mail.resolve_sender(
        _OneRow(_smtp_row()),  # type: ignore[arg-type]
        ACME,
        organisation="Acme Translations",
        reply_to="office@acme.ge",
    )
    assert own is not None
    assert (own.via, own.host, own.port, own.from_email) == (
        "smtp",
        "mail.acme.ge",
        465,
        "office@acme.ge",
    )
    # "Use STARTTLS" off means TLS from the first byte.
    assert (own.ssl, own.starttls, own.password) == (True, False, "s3cret")
    # No name given: the organisation's.
    assert own.from_name == "Acme Translations"


@pytest.mark.parametrize(
    "row",
    [
        None,  # nothing saved, or switched off (the query asks for enabled rows only)
        _smtp_row(host=" "),
        _smtp_row(from_email="not an address"),
        _smtp_row(port="smtp"),
    ],
)
async def test_without_a_usable_server_of_its_own_a_bureau_sends_through_suliko(
    row: IntegrationCredential | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_platform(monkeypatch)
    sender = await business_mail.resolve_sender(
        _OneRow(row),  # type: ignore[arg-type]
        ACME,
        organisation="Acme\r\nBcc: someone@evil.test",
        reply_to="  owner@acme.ge ",
    )
    assert sender is not None
    assert (sender.via, sender.from_email, sender.reply_to) == (
        "platform",
        "noreply@suliko.ge",
        "owner@acme.ge",
    )
    # A name cannot carry a header of its own.
    assert "\n" not in sender.from_name and "\r" not in sender.from_name
    assert sender.from_name.endswith("via Suliko")


async def test_secrets_that_no_longer_decrypt_fall_back_to_suliko(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_platform(monkeypatch)
    row = _smtp_row()
    row.secrets = encrypt_for_tenant(GLOBEX, json.dumps({"password": "x"}))  # another key
    sender = await business_mail.resolve_sender(
        _OneRow(row),  # type: ignore[arg-type]
        ACME,
        organisation="Acme",
        reply_to=None,
    )
    assert sender is not None and sender.via == "platform"


async def test_with_no_server_anywhere_there_is_no_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_platform(monkeypatch, configured=False)
    assert (
        await business_mail.resolve_sender(
            _OneRow(None),  # type: ignore[arg-type]
            ACME,
            organisation="Acme",
            reply_to=None,
        )
        is None
    )
