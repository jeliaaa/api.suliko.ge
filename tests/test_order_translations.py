"""Suliko Translate from an order.

What matters: the person who clicks is the one whose suliko.ge balance is
named, nothing is started that cannot be paid for or read, a finished job's
result is filed under the document exactly once, and a corrected translation
replaces the file it came from without losing it.

suliko.ge is a fake that records what it was asked. Storage is a real
``LocalDiskStorage``; the handlers are called directly, as in the other
order-file tests.
"""

from __future__ import annotations

import io
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

from suliko.api.v1 import order_files, order_translations
from suliko.api.v1.order_translations import (
    LanguageNotAvailableError,
    SaveContentIn,
    TranslateIn,
    match_language,
)
from suliko.config import Settings
from suliko.core.errors import ConflictError, NotFoundError, PayloadTooLargeError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.accounts import UNUSABLE_PASSWORD_HASH
from suliko.integrations.object_storage import LocalDiskStorage
from suliko.integrations.suliko_backend import SulikoUnavailableError
from suliko.integrations.suliko_translate import (
    InsufficientBalanceError,
    JobStatus,
    PreparedFile,
    SulikoAccountMissingError,
    SulikoLanguage,
    TranslatedFile,
    UnsupportedFileError,
)
from suliko.models.directory import Client, ClientType
from suliko.models.order import CopyType, Order, OrderDocument, OrderStatusEvent, Urgency
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind
from suliko.models.reference import DocumentType, Language, TenantSettings
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.translation import DocumentTranslation, TranslationStatus
from suliko.models.user import Account, Role, User

ACME, GLOBEX = 1, 2
NINO, LOCAL = 7, 8  # NINO signs in through suliko.ge; LOCAL has an Office-only password
ORDER, DOCUMENT = 100, 1000

GEORGIAN = SulikoLanguage(1, "Georgian", "ქართული")
ENGLISH = SulikoLanguage(2, "English", "ინგლისური")


@dataclass
class Staff:
    user_id: int = NINO
    tenant_id: int = ACME


@dataclass
class FakeSuliko:
    """suliko.ge, as the routes see it."""

    balance: Decimal | None = Decimal("10")
    pages: int = 3
    offered: list[SulikoLanguage] = field(default_factory=lambda: [GEORGIAN, ENGLISH])
    state: JobStatus = field(default_factory=lambda: JobStatus("processing", 40))
    html: bytes = b"<html><body><p>Hello</p></body></html>"
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    down: bool = False

    enabled = True

    async def aclose(self) -> None:
        return None

    async def languages(self) -> list[SulikoLanguage]:
        return self.offered

    async def prepare(
        self, user_id: str, *, file_name: str, content_type: str, content: bytes
    ) -> PreparedFile:
        self.calls.append(("prepare", user_id, file_name, content))
        return PreparedFile("files/abc", "application/pdf", self.pages, self.balance)

    async def start(self, user_id: str, prepared: PreparedFile, **kwargs: Any) -> str:
        self.calls.append(("start", user_id, kwargs))
        return f"job-{len(self.calls)}"

    async def status(self, job_id: str) -> JobStatus:
        if self.down:
            raise SulikoUnavailableError("down")
        self.calls.append(("status", job_id))
        return self.state

    async def result(self, job_id: str) -> TranslatedFile:
        self.calls.append(("result", job_id))
        return TranslatedFile(self.html, "text/html", "translated.html")

    def named(self, name: str) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == name]


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
            Account,
            User,
            Client,
            DocumentType,
            Language,
            TenantSettings,
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
            session.add_all(
                [
                    Account(
                        id=NINO,
                        email="nino@acme.ge",
                        full_name="Nino",
                        password_hash="x",
                        suliko_user_id="suliko-nino",
                    ),
                    Account(id=LOCAL, email="local@acme.ge", full_name="Local", password_hash="x"),
                ]
            )
            await session.flush()
            for user_id in (NINO, LOCAL):
                session.add(
                    User(
                        id=user_id,
                        tenant_id=ACME,
                        account_id=user_id,
                        username=f"user{user_id}@acme.ge",
                        email=f"user{user_id}@acme.ge",
                        full_name="Someone",
                        password_hash=UNUSABLE_PASSWORD_HASH,
                        role=Role.OWNER,
                        is_active=True,
                    )
                )
            for tenant_id in (ACME, GLOBEX):
                session.add_all(
                    [
                        Client(
                            id=tenant_id, tenant_id=tenant_id, name="C", client_type=ClientType.B2C
                        ),
                        DocumentType(id=tenant_id, tenant_id=tenant_id, name_en="P", name_ka="P"),
                    ]
                )
            session.add_all(
                [
                    Language(tenant_id=ACME, code="ka", name_en="Georgian", name_ka="ქართული"),
                    Language(tenant_id=ACME, code="en", name_en="English", name_ka="ინგლისური"),
                    Language(tenant_id=ACME, code="xx", name_en="Klingon", name_ka="კლინგონური"),
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


async def _source(
    db: AsyncSession,
    storage: LocalDiskStorage,
    content: bytes = b"%PDF source",
    *,
    name: str = "passport scan.pdf",
    kind: FileKind = FileKind.SOURCE,
) -> str:
    upload = UploadFile(
        file=io.BytesIO(content),
        filename=name,
        headers=Headers({"content-type": "application/pdf"}),
    )
    out = await order_files.upload_file(
        ORDER,
        DOCUMENT,
        upload,
        db,
        Staff(),
        storage,
        None,  # type: ignore[arg-type]
        kind=kind,
    )
    return out.id


async def _translate(
    db: AsyncSession,
    storage: LocalDiskStorage,
    suliko: FakeSuliko,
    file_id: str,
    *,
    staff: Staff | None = None,
    rich: bool = False,
) -> order_translations.TranslationOut:
    return await order_translations.translate_file(
        ORDER,
        DOCUMENT,
        file_id,
        TranslateIn(rich=rich),
        db,
        staff or Staff(),  # type: ignore[arg-type]
        storage,
        suliko,
        None,  # type: ignore[arg-type]
    )


async def _look(
    db: AsyncSession, storage: LocalDiskStorage, suliko: FakeSuliko
) -> list[order_translations.TranslationOut]:
    return await order_translations.list_translations(ORDER, DOCUMENT, db, storage, suliko, None)  # type: ignore[arg-type]


# ── Starting ────────────────────────────────────────────────────────────────


async def test_one_click_starts_it_for_the_person_who_clicked(
    db: AsyncSession, tmp_path: Path, audit: list[dict[str, Any]]
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko()
    with tenant_scope(ACME):
        file_id = await _source(db, storage)
        audit.clear()
        out = await _translate(db, storage, suliko, file_id)

    assert (out.status, out.page_count, out.target_language) == (
        TranslationStatus.PROCESSING,
        3,
        "en",
    )
    assert out.source_file_id == file_id and out.result_file_id is None
    # Ten pages before, three for this document.
    assert out.balance == Decimal("7")

    # The file itself went over, in the name of NINO's own suliko.ge account.
    [(_, user_id, file_name, content)] = suliko.named("prepare")
    assert (user_id, file_name, content) == ("suliko-nino", "passport scan.pdf", b"%PDF source")
    [(_, user_id, asked)] = suliko.named("start")
    assert user_id == "suliko-nino"
    assert (asked["target_language_id"], asked["source_language_id"]) == (ENGLISH.id, GEORGIAN.id)
    # Review notes in the bureau's own language.
    assert (asked["output_language_id"], asked["rich"]) == (GEORGIAN.id, False)

    [entry] = audit
    assert entry["action"] == "order.translation_started"
    assert entry["after"]["pages"] == 3


async def test_a_second_click_while_it_runs_is_the_same_job(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko()
    with tenant_scope(ACME):
        file_id = await _source(db, storage)
        first = await _translate(db, storage, suliko, file_id)
        again = await _translate(db, storage, suliko, file_id)

    assert again.id == first.id
    assert len(suliko.named("start")) == 1


async def test_someone_without_a_suliko_account_is_told_and_nothing_is_sent(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko()
    with tenant_scope(ACME):
        file_id = await _source(db, storage)
        with pytest.raises(SulikoAccountMissingError):
            await _translate(db, storage, suliko, file_id, staff=Staff(user_id=LOCAL))
    assert suliko.calls == []


async def test_a_balance_that_does_not_cover_it_starts_nothing(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko(balance=Decimal("2"), pages=3)
    with tenant_scope(ACME):
        file_id = await _source(db, storage)
        with pytest.raises(InsufficientBalanceError) as refused:
            await _translate(db, storage, suliko, file_id)
        rows = (await db.execute(select(DocumentTranslation))).scalars().all()

    assert refused.value.extra == {"pages": 3, "balance": "2"}
    assert suliko.named("start") == [] and rows == []


async def test_a_language_suliko_does_not_offer_is_refused_before_the_upload(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko()
    with tenant_scope(ACME):
        file_id = await _source(db, storage)
        document = await db.get(OrderDocument, DOCUMENT)
        assert document is not None
        document.target_language = "xx"
        await db.flush()
        with pytest.raises(LanguageNotAvailableError):
            await _translate(db, storage, suliko, file_id)
    assert suliko.calls == []


async def test_a_file_too_large_for_suliko_is_refused_here(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        order_translations, "get_settings", lambda: Settings(suliko_translate_max_bytes=5)
    )
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko()
    with tenant_scope(ACME):
        file_id = await _source(db, storage, b"123456")
        with pytest.raises(PayloadTooLargeError):
            await _translate(db, storage, suliko, file_id)
    assert suliko.calls == []


async def test_another_bureaus_file_cannot_be_translated(db: AsyncSession, tmp_path: Path) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko()
    with tenant_scope(ACME):
        file_id = await _source(db, storage)
    with tenant_scope(GLOBEX), pytest.raises(NotFoundError):
        await _translate(db, storage, suliko, file_id, staff=Staff(tenant_id=GLOBEX))
    assert suliko.calls == []


# ── Following ───────────────────────────────────────────────────────────────


async def test_looking_reports_progress_and_files_the_result_once(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko()
    with tenant_scope(ACME):
        file_id = await _source(db, storage)
        await _translate(db, storage, suliko, file_id)

        [running] = await _look(db, storage, suliko)
        assert (running.status, running.progress) == (TranslationStatus.PROCESSING, 40)

        suliko.state = JobStatus("completed", 100)
        [done] = await _look(db, storage, suliko)
        assert done.status is TranslationStatus.COMPLETED and done.result_file_id
        assert done.finished_at is not None and done.progress is None

        # Looking again asks suliko.ge nothing and files nothing more.
        asked = len(suliko.calls)
        [same] = await _look(db, storage, suliko)
        assert same.result_file_id == done.result_file_id and len(suliko.calls) == asked

        files = await order_files.list_files(ORDER, DOCUMENT, db, None)  # type: ignore[arg-type]
        [translation] = [f for f in files if f.kind is FileKind.TRANSLATION]
        assert translation.id == done.result_file_id
        assert (translation.name, translation.content_type) == (
            "passport scan (EN).html",
            "text/html",
        )
        assert translation.uploaded_by == f"suliko:{NINO}"

        response = await order_files.download_file(
            ORDER,
            DOCUMENT,
            translation.id,
            db,
            storage,
            None,  # type: ignore[arg-type]
        )
        assert b"".join([chunk async for chunk in response.body_iterator]) == suliko.html
    assert len(suliko.named("result")) == 1


async def test_a_job_already_claimed_is_not_filed_twice(db: AsyncSession, tmp_path: Path) -> None:
    """Two people looking at the moment it finishes: the second finds it done."""
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko()
    with tenant_scope(ACME):
        file_id = await _source(db, storage)
        await _translate(db, storage, suliko, file_id)
        row = (await db.execute(select(DocumentTranslation))).scalar_one()
        order = await db.get(Order, ORDER)
        document = await db.get(OrderDocument, DOCUMENT)
        assert order is not None and document is not None

        await order_translations._collect(db, storage, suliko, row, order, document)
        await order_translations._collect(db, storage, suliko, row, order, document)

        files = await order_files.list_files(ORDER, DOCUMENT, db, None)  # type: ignore[arg-type]
    assert len([f for f in files if f.kind is FileKind.TRANSLATION]) == 1
    assert len(suliko.named("result")) == 1


async def test_a_failed_job_says_so_without_sulikos_own_words(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko()
    with tenant_scope(ACME):
        file_id = await _source(db, storage)
        await _translate(db, storage, suliko, file_id)
        suliko.state = JobStatus("failed", 0, "Gemini 429 at node gke-prod-7")
        [failed] = await _look(db, storage, suliko)

        assert failed.status is TranslationStatus.FAILED
        assert failed.error and "gke-prod-7" not in failed.error
        # It can be started again: the failed one no longer counts as running.
        again = await _translate(db, storage, suliko, file_id)
    assert again.id != failed.id


async def test_suliko_being_down_leaves_a_running_job_running(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko()
    with tenant_scope(ACME):
        file_id = await _source(db, storage)
        await _translate(db, storage, suliko, file_id)
        suliko.down = True
        [still] = await _look(db, storage, suliko)
    assert still.status is TranslationStatus.PROCESSING


# ── Correcting ──────────────────────────────────────────────────────────────


async def test_saving_a_correction_replaces_the_file_and_keeps_the_record(
    db: AsyncSession, tmp_path: Path, audit: list[dict[str, Any]]
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    suliko = FakeSuliko(state=JobStatus("completed", 100))
    with tenant_scope(ACME):
        file_id = await _source(db, storage)
        await _translate(db, storage, suliko, file_id)
        [done] = await _look(db, storage, suliko)
        assert done.result_file_id is not None
        audit.clear()

        saved = await order_translations.save_translation(
            ORDER,
            DOCUMENT,
            done.result_file_id,
            SaveContentIn(html="<p>Hello, corrected</p>"),
            db,
            Staff(),  # type: ignore[arg-type]
            storage,
            None,  # type: ignore[arg-type]
        )
        assert saved.id != done.result_file_id
        assert (saved.name, saved.uploaded_by) == ("passport scan (EN).html", f"user:{NINO}")

        files = await order_files.list_files(ORDER, DOCUMENT, db, None)  # type: ignore[arg-type]
        assert [f.id for f in files if f.kind is FileKind.TRANSLATION] == [saved.id]
        # The old one is hidden, not erased, and the record follows the new one.
        with pytest.raises(NotFoundError):
            await order_files.download_file(
                ORDER,
                DOCUMENT,
                done.result_file_id,
                db,
                storage,
                None,  # type: ignore[arg-type]
            )
        [after] = await _look(db, storage, suliko)
        assert after.result_file_id == saved.id
    assert [entry["action"] for entry in audit] == ["order.translation_edited"]


async def test_only_a_translation_made_here_can_be_saved_over(
    db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        source = await _source(db, storage)
        uploaded_pdf = await _source(db, storage, name="done.pdf", kind=FileKind.TRANSLATION)
        for file_id in (source, uploaded_pdf):
            with pytest.raises(ConflictError):
                await order_translations.save_translation(
                    ORDER,
                    DOCUMENT,
                    file_id,
                    SaveContentIn(html="<p>x</p>"),
                    db,
                    Staff(),  # type: ignore[arg-type]
                    storage,
                    None,  # type: ignore[arg-type]
                )


# ── Which language ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "name_en", "name_ka", "expected"),
    [
        ("en", "English", "ინგლისური", ENGLISH),
        ("en", "  english ", "", ENGLISH),
        # A bureau that renamed its language is still matched by its code...
        ("ka", "Georgian (formal)", "", GEORGIAN),
        # ...or by the Georgian name.
        ("zz", "Ingliseli", "ინგლისური", ENGLISH),
        ("xx", "Klingon", "კლინგონური", None),
    ],
)
def test_languages_are_matched_by_name_then_code(
    code: str, name_en: str, name_ka: str, expected: SulikoLanguage | None
) -> None:
    assert match_language(code, name_en, name_ka, [GEORGIAN, ENGLISH]) == expected


#: suliko.ge's languages as its public list gave them on 2026-10-10, spelling
#: and all: most names end in "Language", two Georgian ones in "ენა", Finnish
#: is "Finish", and Hebrew and Slovenian are written differently from Office.
SULIKO_GE_LANGUAGES = [
    SulikoLanguage(1, "Georgian Language", "ქართული"),
    SulikoLanguage(2, "English Language", "ინგლისური"),
    SulikoLanguage(4, "Latvian Language", "ლატვიური"),
    SulikoLanguage(5, "Slovenian Language", "სლოვენიური"),
    SulikoLanguage(6, "Azerbaijani Language", "აზერბაიჯანული"),
    SulikoLanguage(7, "Turkish Language", "თურქული"),
    SulikoLanguage(8, "German Language", "გერმანული"),
    SulikoLanguage(9, "Armenian Language", "სომხური"),
    SulikoLanguage(13, "Italian Language", "იტალიური"),
    SulikoLanguage(12, "French Language", "ფრანგული"),
    SulikoLanguage(15, "Latin", "ლათინური"),
    SulikoLanguage(17, "Japanese", "იაპონური"),
    SulikoLanguage(18, "Chinese", "ჩინური"),
    SulikoLanguage(19, "Serbian language", "სერბული"),
    SulikoLanguage(20, "Urdu Language", "ურდუ"),
    SulikoLanguage(21, "Spanish Language", "ესპანური"),
    SulikoLanguage(3, "Greek Language", "ბერძნული"),
    SulikoLanguage(11, "Slovak Language", "სლოვაკური"),
    SulikoLanguage(16, "Russian Language", "რუსული"),
    SulikoLanguage(22, "Hebrew Language", "ივრითი"),
    SulikoLanguage(23, "Portuguese Language", "პორტუგალიური"),
    SulikoLanguage(24, "Finish Language", "ფინური"),
    SulikoLanguage(31, "Ukrainian Language", "უკრაინული"),
    SulikoLanguage(32, "Polish Language", "პოლონური ენა"),
    SulikoLanguage(33, "Arabic Language", "არაბული ენა"),
    SulikoLanguage(34, "Romanian Language", "რუმინული"),
]

#: Which of them each language Office ships with is.
SEEDED_TO_SULIKO_GE = {
    "ka": 1,
    "en": 2,
    "ru": 16,
    "de": 8,
    "fr": 12,
    "it": 13,
    "es": 21,
    "pt": 23,
    "tr": 7,
    "az": 6,
    "hy": 9,
    "uk": 31,
    "pl": 32,
    "ar": 33,
    "he": 22,
    "zh": 18,
    "ja": 17,
    "el": 3,
    "lv": 4,
    "sl": 5,
    "sk": 11,
    "sr": 19,
    "ur": 20,
    "fi": 24,
    "la": 15,
}


def test_every_language_office_ships_with_finds_its_own_on_suliko_ge() -> None:
    from suliko.domain.reference_seed import LANGUAGES

    assert {code for code, _, _ in LANGUAGES} == set(SEEDED_TO_SULIKO_GE)
    for code, name_en, name_ka in LANGUAGES:
        found = match_language(code, name_en, name_ka, SULIKO_GE_LANGUAGES)
        assert found is not None, code
        assert found.id == SEEDED_TO_SULIKO_GE[code], code


@pytest.mark.parametrize(
    ("code", "name_en", "name_ka", "expected_id"),
    [
        # By the English name alone, with suliko.ge's "Language" set aside.
        ("zz", "Polish", "", 32),
        # By the Georgian name alone, with its "ენა" set aside.
        ("zz", "", "არაბული", 33),
        # A renamed language, by its code. suliko.ge's own spelling of Finnish.
        ("fi", "Suomi", "", 24),
        ("ro", "Rumanian", "", 34),
        # A word that only looks like the suffix is not dropped from the middle.
        ("zz", "Language", "", None),
        ("xx", "Klingon", "კლინგონური", None),
    ],
)
def test_names_are_matched_as_suliko_ge_really_writes_them(
    code: str, name_en: str, name_ka: str, expected_id: int | None
) -> None:
    found = match_language(code, name_en, name_ka, SULIKO_GE_LANGUAGES)
    assert (found.id if found else None) == expected_id


def test_the_unsupported_file_error_is_one_the_form_can_tell_apart() -> None:
    assert UnsupportedFileError("x").error_code == "unsupported_file"
    assert InsufficientBalanceError("x").status_code == 402
