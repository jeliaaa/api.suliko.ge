"""Correcting a translation in the Word editor: opening it and saving it back.

The handlers are called directly, as in the other order-file tests, with a real
``LocalDiskStorage`` and a fake for suliko.ge's backend (the editor's server).
What matters: only a Word or HTML translation opens, and it goes to the editor
under the right name; a document the editor's server cannot read, or cannot be
reached for, is told apart; a saved document must be a real .docx without
macros, replaces the file it came from without losing it, and takes the
translation's record along; and nothing crosses to another bureau.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from suliko.api.v1 import order_files, order_word_editor
from suliko.config import Settings
from suliko.core.errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain import word_file
from suliko.domain.translation_docx import DOCX_CONTENT_TYPE, html_to_docx
from suliko.domain.word_file import InvalidWordDocumentError, ensure_word_document
from suliko.integrations.object_storage import LocalDiskStorage
from suliko.integrations.suliko_backend import SulikoUnavailableError
from suliko.integrations.word_editor import (
    EditorDocumentUnreadableError,
    HttpWordEditorImporter,
    UnconfiguredWordEditorImporter,
)
from suliko.models.directory import Client, ClientType
from suliko.models.order import CopyType, Order, OrderDocument, OrderStatusEvent, Urgency
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind
from suliko.models.reference import DocumentType
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.translation import DocumentTranslation, TranslationStatus

ACME, GLOBEX = 1, 2
ORDER, DOCUMENT = 100, 1000

WORD = html_to_docx("<h1>Passport</h1><p>გამარჯობა</p>")


@dataclass
class Staff:
    user_id: int = 7
    tenant_id: int = ACME


@dataclass
class FakeEditorServer:
    """suliko.ge's backend as the editor routes see it."""

    answer: str = '{"sfdt": "abc"}'
    error: Exception | None = None
    calls: list[tuple[str, bytes]] = field(default_factory=list)

    async def to_sfdt(self, *, file_name: str, content: bytes) -> str:
        self.calls.append((file_name, content))
        if self.error:
            raise self.error
        return self.answer

    async def aclose(self) -> None:
        return None


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


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    tables = [
        m.__table__
        for m in (
            Tenant,
            Client,
            DocumentType,
            Order,
            OrderDocument,
            OrderFile,
            OrderStatusEvent,
            DocumentTranslation,
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
                            id=tenant_id, tenant_id=tenant_id, name="C", client_type=ClientType.B2C
                        ),
                        DocumentType(id=tenant_id, tenant_id=tenant_id, name_en="P", name_ka="P"),
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
                        urgency=Urgency.STANDARD,
                    )
                )
            await session.flush()
            for tenant_id in (ACME, GLOBEX):
                session.add(
                    OrderDocument(
                        id=tenant_id * 1000,
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
                    )
                )
            await session.commit()
        yield session
    await engine.dispose()


async def _file(
    db: AsyncSession,
    storage: LocalDiskStorage,
    *,
    name: str,
    content: bytes,
    content_type: str,
    kind: FileKind = FileKind.TRANSLATION,
) -> str:
    out = await order_files.upload_file(
        ORDER,
        DOCUMENT,
        UploadFile(
            file=io.BytesIO(content),
            filename=name,
            headers=Headers({"content-type": content_type}),
        ),
        db,
        Staff(),
        storage,
        None,  # type: ignore[arg-type]
        kind=kind,
    )
    return out.id


async def _record_for(db: AsyncSession, file_id: str, html: str | None = "<p>old</p>") -> None:
    row = (await db.execute(select(OrderFile).where(OrderFile.public_id == file_id))).scalar_one()
    db.add(
        DocumentTranslation(
            public_id="job-0000001",
            order_document_id=DOCUMENT,
            status=TranslationStatus.COMPLETED,
            result_file_id=row.id,
            suliko_job_id="j",
            suliko_user_id="u",
            target_language="en",
            page_count=1,
            html=html,
        )
    )
    await db.flush()


def _upload(content: bytes, name: str = "translation.docx") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=name,
        headers=Headers({"content-type": DOCX_CONTENT_TYPE}),
    )


async def _open(
    db: AsyncSession, storage: LocalDiskStorage, server: FakeEditorServer, file_id: str
) -> order_word_editor.EditorDocumentOut:
    return await order_word_editor.open_in_editor(
        ORDER,
        DOCUMENT,
        file_id,
        db,
        storage,
        server,
        None,  # type: ignore[arg-type]
    )


async def _save(
    db: AsyncSession, storage: LocalDiskStorage, file_id: str, content: bytes
) -> order_files.StaffFileOut:
    return await order_word_editor.save_word_document(
        ORDER,
        DOCUMENT,
        file_id,
        _upload(content),
        db,
        Staff(),  # type: ignore[arg-type]
        storage,
        None,  # type: ignore[arg-type]
    )


def _docx_with(*extra: str) -> bytes:
    """A zip with the parts of a .docx, and any more that are named."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        for name in extra:
            archive.writestr(name, "x")
    return out.getvalue()


# ── Opening ─────────────────────────────────────────────────────────────────


async def test_a_word_translation_goes_to_the_editor_as_a_docx(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    server = FakeEditorServer()
    with tenant_scope(ACME):
        file_id = await _file(
            db, storage, name="passport (EN).docx", content=WORD, content_type=DOCX_CONTENT_TYPE
        )
        opened = await _open(db, storage, server, file_id)
    assert opened.sfdt == server.answer and opened.file_name == "passport (EN).docx"
    assert server.calls == [("translation.docx", WORD)]


async def test_an_html_translation_goes_to_the_editor_as_html(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    server = FakeEditorServer()
    html = "<p>ძველი</p>".encode()
    with tenant_scope(ACME):
        file_id = await _file(
            db, storage, name="old (EN).html", content=html, content_type="text/html"
        )
        await _open(db, storage, server, file_id)
    assert server.calls == [("translation.html", html)]


async def test_whoever_added_a_word_translation_it_is_edited_the_same(
    db: AsyncSession, tmp_path: Path
) -> None:
    """A translator's own .docx, with no record behind it, opens like any other."""
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _file(
            db, storage, name="from-ana.docx", content=WORD, content_type=DOCX_CONTENT_TYPE
        )
        files = await order_files.list_files(ORDER, DOCUMENT, db, None)  # type: ignore[arg-type]
        assert [(f.id, f.editable) for f in files] == [(file_id, True)]
        await _open(db, storage, FakeEditorServer(), file_id)


async def test_nothing_else_opens_and_the_editors_server_is_not_asked(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    server = FakeEditorServer()
    with tenant_scope(ACME):
        source = await _file(
            db,
            storage,
            name="scan.docx",
            content=WORD,
            content_type=DOCX_CONTENT_TYPE,
            kind=FileKind.SOURCE,
        )
        pdf = await _file(
            db, storage, name="done.pdf", content=b"%PDF", content_type="application/pdf"
        )
        for file_id in (source, pdf):
            with pytest.raises(NotFoundError):
                await _open(db, storage, server, file_id)
    assert server.calls == []


async def test_a_document_the_editors_server_cannot_read_is_told_apart_from_one_it_cannot_reach(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        file_id = await _file(
            db, storage, name="t.docx", content=WORD, content_type=DOCX_CONTENT_TYPE
        )
        with pytest.raises(EditorDocumentUnreadableError):
            await _open(
                db, storage, FakeEditorServer(error=EditorDocumentUnreadableError("x")), file_id
            )
        with pytest.raises(SulikoUnavailableError):
            await _open(db, storage, FakeEditorServer(error=SulikoUnavailableError("x")), file_id)


async def test_a_document_too_large_for_the_editor_is_refused_before_it_is_sent(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        order_word_editor, "get_settings", lambda: Settings(word_editor_max_bytes=10)
    )
    storage = LocalDiskStorage(str(tmp_path))
    server = FakeEditorServer()
    with tenant_scope(ACME):
        file_id = await _file(
            db, storage, name="t.docx", content=WORD, content_type=DOCX_CONTENT_TYPE
        )
        with pytest.raises(PayloadTooLargeError):
            await _open(db, storage, server, file_id)
    assert server.calls == []


async def test_another_bureau_cannot_open_the_translation(db: AsyncSession, tmp_path: Path) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    server = FakeEditorServer()
    with tenant_scope(ACME):
        file_id = await _file(
            db, storage, name="t.docx", content=WORD, content_type=DOCX_CONTENT_TYPE
        )
    with tenant_scope(GLOBEX):
        for order_id, document_id in ((200, 2000), (ORDER, DOCUMENT)):
            with pytest.raises(NotFoundError):
                await order_word_editor.open_in_editor(
                    order_id,
                    document_id,
                    file_id,
                    db,
                    storage,
                    server,
                    None,  # type: ignore[arg-type]
                )
    assert server.calls == []


# ── Saving ──────────────────────────────────────────────────────────────────


async def test_a_saved_document_replaces_the_file_and_the_record_follows(
    db: AsyncSession, tmp_path: Path, audit: list[dict[str, Any]]
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    edited = html_to_docx("<h1>Passport</h1><p>გასწორებული</p>")
    with tenant_scope(ACME):
        old = await _file(
            db, storage, name="passport (EN).docx", content=WORD, content_type=DOCX_CONTENT_TYPE
        )
        await _record_for(db, old)
        audit.clear()

        saved = await _save(db, storage, old, edited)

        assert saved.id != old
        assert (saved.name, saved.content_type) == ("passport (EN).docx", DOCX_CONTENT_TYPE)
        assert (saved.uploaded_by, saved.editable) == ("user:7", True)
        response = await order_files.download_file(
            ORDER,
            DOCUMENT,
            saved.id,
            db,
            storage,
            None,  # type: ignore[arg-type]
        )
        assert b"".join([chunk async for chunk in response.body_iterator]) == edited

        files = await order_files.list_files(ORDER, DOCUMENT, db, None)  # type: ignore[arg-type]
        assert [f.id for f in files] == [saved.id]
        # Hidden, not erased.
        with pytest.raises(NotFoundError):
            await order_files.download_file(ORDER, DOCUMENT, old, db, storage, None)  # type: ignore[arg-type]

        # The record follows the new file, and the HTML it kept is of the
        # translation as it was made, so it goes.
        [record] = (await db.execute(select(DocumentTranslation))).scalars().all()
        new_row = (
            await db.execute(select(OrderFile).where(OrderFile.public_id == saved.id))
        ).scalar_one()
        assert (record.result_file_id, record.html) == (new_row.id, None)
    assert [(e["action"], e["after"]["editor"]) for e in audit] == [
        ("order.translation_edited", "word")
    ]


async def test_an_html_translation_becomes_a_word_file_when_saved_from_the_editor(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        old = await _file(
            db, storage, name="old (EN).html", content=b"<p>x</p>", content_type="text/html"
        )
        saved = await _save(db, storage, old, WORD)
    assert (saved.name, saved.content_type) == ("old (EN).docx", DOCX_CONTENT_TYPE)


async def test_only_a_real_word_document_is_kept(db: AsyncSession, tmp_path: Path) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        old = await _file(db, storage, name="t.docx", content=WORD, content_type=DOCX_CONTENT_TYPE)
        for bad in (
            b"not a zip at all",
            _docx_with()[:-30],  # cut short
            _docx_with("word/vbaProject.bin"),
            b"",
        ):
            # Not a Word document, or no document at all (an empty upload).
            with pytest.raises(ValidationError):
                await _save(db, storage, old, bad)
        # Nothing was replaced by any of them.
        files = await order_files.list_files(ORDER, DOCUMENT, db, None)  # type: ignore[arg-type]
        assert [f.id for f in files] == [old]


async def test_only_a_word_or_html_translation_can_be_saved_over(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        source = await _file(
            db,
            storage,
            name="scan.docx",
            content=WORD,
            content_type=DOCX_CONTENT_TYPE,
            kind=FileKind.SOURCE,
        )
        pdf = await _file(
            db, storage, name="done.pdf", content=b"%PDF", content_type="application/pdf"
        )
        for file_id in (source, pdf):
            with pytest.raises(ConflictError):
                await _save(db, storage, file_id, WORD)


async def test_a_saved_document_over_the_limit_is_refused(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        old = await _file(db, storage, name="t.docx", content=WORD, content_type=DOCX_CONTENT_TYPE)
        monkeypatch.setattr(
            order_word_editor, "get_settings", lambda: Settings(word_editor_max_bytes=100)
        )
        with pytest.raises(PayloadTooLargeError):
            await _save(db, storage, old, WORD)


async def test_another_bureau_cannot_save_over_the_translation(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        old = await _file(db, storage, name="t.docx", content=WORD, content_type=DOCX_CONTENT_TYPE)
    with tenant_scope(GLOBEX):
        for order_id, document_id in ((200, 2000), (ORDER, DOCUMENT)):
            with pytest.raises(NotFoundError):
                await order_word_editor.save_word_document(
                    order_id,
                    document_id,
                    old,
                    _upload(WORD),
                    db,
                    Staff(2, GLOBEX),  # type: ignore[arg-type]
                    storage,
                    None,  # type: ignore[arg-type]
                )


# ── What a Word document is ─────────────────────────────────────────────────


def test_a_real_word_document_passes() -> None:
    ensure_word_document(WORD)
    ensure_word_document(_docx_with("word/styles.xml", "word/media/image1.png"))


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"PK not really",
        b"%PDF-1.7",
        pytest.param(_docx_with("word/vbaProject.bin"), id="macros"),
        pytest.param(_docx_with("Word/VBAProject.bin"), id="macros, other case"),
    ],
)
def test_anything_else_is_refused(content: bytes) -> None:
    with pytest.raises(InvalidWordDocumentError):
        ensure_word_document(content)


def test_a_zip_without_the_parts_of_a_document_is_refused() -> None:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("readme.txt", "hello")
    with pytest.raises(InvalidWordDocumentError):
        ensure_word_document(out.getvalue())


def test_a_zip_made_to_swell_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(word_file, "MAX_UNPACKED_BYTES", 10)
    with pytest.raises(InvalidWordDocumentError, match="too large"):
        ensure_word_document(WORD)
    monkeypatch.setattr(word_file, "MAX_UNPACKED_BYTES", 10**9)
    monkeypatch.setattr(word_file, "MAX_PARTS", 3)
    with pytest.raises(InvalidWordDocumentError, match="too large"):
        ensure_word_document(WORD)


# ── The client for suliko.ge's backend ──────────────────────────────────────


async def test_the_unconfigured_importer_says_the_editor_is_not_there() -> None:
    with pytest.raises(SulikoUnavailableError):
        await UnconfiguredWordEditorImporter().to_sfdt(file_name="t.docx", content=b"x")


@pytest.mark.parametrize(
    ("status", "raises"),
    [
        (400, EditorDocumentUnreadableError),
        (413, EditorDocumentUnreadableError),
        (422, EditorDocumentUnreadableError),
        # The key is wrong or the endpoint is not there yet: not the file's fault.
        (401, SulikoUnavailableError),
        (404, SulikoUnavailableError),
        (500, SulikoUnavailableError),
    ],
)
async def test_the_editors_answers_are_told_apart(status: int, raises: type[Exception]) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text='{"error":"x"}')

    importer = HttpWordEditorImporter(
        "https://suliko.example",
        "k" * 32,
        5,
        client=httpx.AsyncClient(
            base_url="https://suliko.example", transport=httpx.MockTransport(handler)
        ),
    )
    with pytest.raises(raises):
        await importer.to_sfdt(file_name="t.docx", content=b"x")


async def test_the_importer_sends_the_key_and_the_file_and_returns_the_json() -> None:
    import httpx

    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["key"] = request.headers.get("x-office-key")
        seen["body"] = request.content
        return httpx.Response(200, text='{"sfdt":"abc"}')

    importer = HttpWordEditorImporter(
        "https://suliko.example",
        "secret-key-of-enough-length-here",
        5,
        client=httpx.AsyncClient(
            base_url="https://suliko.example", transport=httpx.MockTransport(handler)
        ),
    )
    assert (
        await importer.to_sfdt(file_name="translation.docx", content=b"BYTES") == '{"sfdt":"abc"}'
    )
    assert seen["path"] == "/api/office/document-editor/import"
    assert seen["key"] == "secret-key-of-enough-length-here"
    assert b'filename="translation.docx"' in seen["body"] and b"BYTES" in seen["body"]


async def test_an_importer_with_no_key_is_unavailable_not_refused() -> None:
    import httpx

    importer = HttpWordEditorImporter(
        "https://suliko.example",
        "",
        5,
        client=httpx.AsyncClient(
            base_url="https://suliko.example",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="{}")),
        ),
    )
    with pytest.raises(SulikoUnavailableError):
        await importer.to_sfdt(file_name="t.docx", content=b"x")
