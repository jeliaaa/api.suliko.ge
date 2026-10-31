"""`drive_mirror.sync_order` against a SQLite copy of the schema and a fake Drive:
what gets copied where, that a file is copied once, and what a failure leaves."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain import drive_mirror
from suliko.domain.order_files import upload_document_file
from suliko.integrations.google_drive import DriveError, DriveFolder
from suliko.integrations.object_storage import LocalDiskStorage
from suliko.models.directory import Client, ClientType
from suliko.models.order import CopyType, Order, OrderDocument, Urgency
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind
from suliko.models.reference import DocumentType, Language
from suliko.models.tenant import Tenant, TenantStatus

TENANT = 1


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


class FakeDrive:
    """Folders are named by their path; uploads are recorded as (folder, name)."""

    def __init__(self) -> None:
        self.folders: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.fail_uploads = False

    async def child_folder(self, parent_id: str, name: str) -> DriveFolder:
        path = f"{parent_id}/{name}"
        if path not in self.folders:
            self.folders.append(path)
        return DriveFolder(id=path, name=name)

    async def upload_file(
        self, parent_id: str, name: str, content: bytes, content_type: str
    ) -> str:
        if self.fail_uploads:
            raise DriveError("Google Drive refused the upload (PERMISSION_DENIED).")
        self.uploads.append((parent_id, name))
        return f"drive-{len(self.uploads)}"

    async def aclose(self) -> None:
        return None


@pytest_asyncio.fixture
async def maker(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    tables = [
        m.__table__
        for m in (Tenant, Client, DocumentType, Language, Order, OrderDocument, OrderFile)
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    session_maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session
            await session.commit()

    monkeypatch.setattr(drive_mirror, "session_scope", scope)

    async with session_maker() as session:
        with bypass_tenant_scope():
            session.add(
                Tenant(
                    id=TENANT,
                    slug="acme",
                    display_name="acme",
                    status=TenantStatus.ACTIVE,
                    locale="ka",
                )
            )
            await session.flush()
            session.add_all(
                [
                    Client(id=1, tenant_id=TENANT, name="Acme Ltd", client_type=ClientType.B2C),
                    DocumentType(id=1, tenant_id=TENANT, name_en="P", name_ka="P"),
                    Language(tenant_id=TENANT, code="ka", name_en="Georgian", name_ka="ქ"),
                    Language(tenant_id=TENANT, code="ru", name_en="Russian", name_ka="რ"),
                ]
            )
            await session.flush()
            session.add(
                Order(
                    id=100,
                    tenant_id=TENANT,
                    client_id=1,
                    order_date=date(2026, 10, 30),
                    urgency=Urgency.STANDARD,
                    number=1434,
                )
            )
            await session.flush()
            session.add(
                OrderDocument(
                    id=1000,
                    tenant_id=TENANT,
                    order_id=100,
                    document_type_id=1,
                    source_language="ru",
                    target_language="ka",
                    page_count=1,
                    copy_type=CopyType.ORIGINAL,
                    price=Decimal("10"),
                    translator_cost=Decimal("0"),
                    notary_cost=Decimal("0"),
                )
            )
            await session.commit()
    yield session_maker
    await engine.dispose()


async def add_file(maker: async_sessionmaker[AsyncSession], storage, name: str, kind: FileKind):
    async with maker() as db:
        with tenant_scope(TENANT):
            order = await db.get(Order, 100)
            document = await db.get(OrderDocument, 1000)
            await upload_document_file(
                db,
                storage,
                order=order,
                document=document,
                kind=kind,
                file_name=name,
                content=b"bytes of " + name.encode(),
                content_type="application/pdf",
                uploaded_by="user:1",
            )
            await db.commit()


def connected(monkeypatch: pytest.MonkeyPatch, fake: FakeDrive | None) -> None:
    async def connect(_db, _tenant_id):
        return drive_mirror.Connection(drive=fake, root_folder_id="root") if fake else None

    monkeypatch.setattr(drive_mirror, "connect", connect)


async def test_sources_and_translations_go_to_their_own_folders(
    maker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    await add_file(maker, storage, "a.pdf", FileKind.SOURCE)
    await add_file(maker, storage, "a_Translated.docx", FileKind.TRANSLATION)
    fake = FakeDrive()
    connected(monkeypatch, fake)

    result = await drive_mirror.sync_order(TENANT, 100, storage)

    order_folder = "root/30.10.2026 - #001434 - Acme Ltd"
    assert (result.copied, result.failed) == (2, 0)
    assert fake.uploads == [
        (f"{order_folder}/1. Source Documents", "a.pdf"),
        (f"{order_folder}/2. Translated", "a_Translated.docx"),
    ]
    async with maker() as db:
        with tenant_scope(TENANT):
            order = await db.get(Order, 100)
            assert order.google_drive_folder == order_folder


async def test_a_second_sync_copies_nothing_again(
    maker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    await add_file(maker, storage, "a.pdf", FileKind.SOURCE)
    fake = FakeDrive()
    connected(monkeypatch, fake)

    await drive_mirror.sync_order(TENANT, 100, storage)
    again = await drive_mirror.sync_order(TENANT, 100, storage)

    assert again.copied == 0
    assert len(fake.uploads) == 1


async def test_a_failed_copy_is_kept_for_the_next_try(
    maker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    await add_file(maker, storage, "a.pdf", FileKind.SOURCE)
    fake = FakeDrive()
    fake.fail_uploads = True
    connected(monkeypatch, fake)

    first = await drive_mirror.sync_order(TENANT, 100, storage)
    assert (first.copied, first.failed) == (0, 1)
    async with maker() as db:
        with tenant_scope(TENANT):
            row = (await db.execute(select(OrderFile))).scalar_one()
            assert row.drive_file_id is None and "PERMISSION_DENIED" in row.drive_error

    fake.fail_uploads = False
    second = await drive_mirror.sync_order(TENANT, 100, storage)
    assert second.copied == 1
    async with maker() as db:
        with tenant_scope(TENANT):
            row = (await db.execute(select(OrderFile))).scalar_one()
            assert row.drive_file_id and row.drive_error is None


async def test_an_organisation_without_drive_is_left_alone(
    maker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    await add_file(maker, storage, "a.pdf", FileKind.SOURCE)
    connected(monkeypatch, None)

    result = await drive_mirror.sync_order(TENANT, 100, storage)

    assert result.connected is False and result.copied == 0
