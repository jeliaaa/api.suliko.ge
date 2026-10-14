"""The CRM's file routes, end to end against the real local-disk backend.

The handlers are called directly with a staff session stand-in, the way
``test_user_management.py`` calls its handlers. Storage is a real
``LocalDiskStorage`` in a temporary directory — the backend production runs
with on a single server — so a byte that goes in is read back off the disk.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from suliko.api.v1 import order_files
from suliko.api.v1.order_files import StoredInVaultError
from suliko.core.errors import ConflictError, NotFoundError, UpstreamUnavailableError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.order_files import purge_removed_files, purge_working_copies
from suliko.integrations.object_storage import LocalDiskStorage, VaultStorage
from suliko.models.directory import Client, ClientType
from suliko.models.order import CopyType, Order, OrderDocument, OrderStatusEvent, Urgency
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind
from suliko.models.reference import DocumentType
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.translation import DocumentTranslation, TranslationStatus

ACME, GLOBEX = 1, 2


@dataclass
class Staff:
    user_id: int
    tenant_id: int


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


def _upload(name: str, content: bytes, content_type: str = "application/pdf") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=name,
        headers=Headers({"content-type": content_type}),
    )


async def _body(response: Any) -> bytes:
    return b"".join([chunk async for chunk in response.body_iterator])


async def test_upload_list_download_remove(
    db: AsyncSession, tmp_path: Path, audit: list[dict[str, Any]]
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    staff = Staff(user_id=7, tenant_id=ACME)
    scan = b"%PDF-1.7 " + b"x" * 200_000

    with tenant_scope(ACME):
        out = await order_files.upload_file(
            100,
            1000,
            _upload("..\\..\\passport\x07.pdf", scan),
            db,
            staff,
            storage,
            None,  # type: ignore[arg-type]
            kind=FileKind.SOURCE,
        )
        # The caller's path and control characters are gone from the name.
        assert out.name == "passport.pdf"
        assert out.uploaded_by == "user:7"
        assert out.size_bytes == len(scan)
        assert [e["action"] for e in audit] == ["order.file_uploaded"]

        # On disk under the tenant, order and document — not under its name.
        on_disk = tmp_path / f"tenants/{ACME}/orders/100/documents/1000/{out.id}"
        assert on_disk.read_bytes() == scan

        listed = await order_files.list_files(100, 1000, db, None)  # type: ignore[arg-type]
        assert [f.id for f in listed] == [out.id]

        response = await order_files.download_file(100, 1000, out.id, db, storage, None)  # type: ignore[arg-type]
        assert await _body(response) == scan
        assert response.headers["content-length"] == str(len(scan))
        assert response.headers["content-disposition"].startswith("attachment;")

        await order_files.delete_file(100, 1000, out.id, db, staff, None)  # type: ignore[arg-type]
        assert await order_files.list_files(100, 1000, db, None) == []  # type: ignore[arg-type]
        with pytest.raises(NotFoundError):
            await order_files.download_file(100, 1000, out.id, db, storage, None)  # type: ignore[arg-type]
        # Hidden, not erased: the purge removes the bytes later.
        assert on_disk.exists()


async def test_a_file_moves_to_the_other_side_and_takes_a_new_name(
    db: AsyncSession, tmp_path: Path, audit: list[dict[str, Any]]
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    staff = Staff(user_id=7, tenant_id=ACME)
    scan = b"%PDF-1.7 scan"

    with tenant_scope(ACME):
        out = await order_files.upload_file(
            100,
            1000,
            _upload("scan.pdf", scan),
            db,
            staff,
            storage,
            None,  # type: ignore[arg-type]
            kind=FileKind.SOURCE,
        )
        on_disk = tmp_path / f"tenants/{ACME}/orders/100/documents/1000/{out.id}"

        moved = await order_files.update_file(
            100,
            1000,
            out.id,
            order_files.FileChange(kind=FileKind.TRANSLATION),
            db,
            staff,
            None,  # type: ignore[arg-type]
        )
        assert moved.kind is FileKind.TRANSLATION
        assert moved.name == "scan.pdf"

        # The extension stays, whatever is typed; a path does not get in.
        renamed = await order_files.update_file(
            100,
            1000,
            out.id,
            order_files.FileChange(name="..\\..\\passport, signed"),
            db,
            staff,
            None,  # type: ignore[arg-type]
        )
        assert renamed.name == "passport, signed.pdf"
        assert renamed.kind is FileKind.TRANSLATION

        # Same id, same bytes, same place on disk: only the row changed.
        listed = await order_files.list_files(100, 1000, db, None)  # type: ignore[arg-type]
        assert [(f.id, f.kind, f.name) for f in listed] == [
            (out.id, FileKind.TRANSLATION, "passport, signed.pdf")
        ]
        assert on_disk.read_bytes() == scan

        assert [e["action"] for e in audit] == [
            "order.file_uploaded",
            "order.file_changed",
            "order.file_changed",
        ]
        assert audit[1]["before"]["kind"] == "source"
        assert audit[1]["after"]["kind"] == "translation"

        # Asking for what is already so writes no audit line.
        await order_files.update_file(
            100,
            1000,
            out.id,
            order_files.FileChange(kind=FileKind.TRANSLATION),
            db,
            staff,
            None,  # type: ignore[arg-type]
        )
        assert len(audit) == 3


def test_a_file_change_must_change_something() -> None:
    with pytest.raises(ValueError, match="Nothing to change"):
        order_files.FileChange()


async def test_another_bureau_cannot_change_the_file(db: AsyncSession, tmp_path: Path) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        out = await order_files.upload_file(
            100,
            1000,
            _upload("a.pdf", b"acme"),
            db,
            Staff(1, ACME),
            storage,
            None,  # type: ignore[arg-type]
        )

    with tenant_scope(GLOBEX):
        for order_id, document_id in ((200, 2000), (100, 1000)):
            with pytest.raises(NotFoundError):
                await order_files.update_file(
                    order_id,
                    document_id,
                    out.id,
                    order_files.FileChange(name="theirs.pdf"),
                    db,
                    Staff(2, GLOBEX),
                    None,  # type: ignore[arg-type]
                )

    with tenant_scope(ACME):
        listed = await order_files.list_files(100, 1000, db, None)  # type: ignore[arg-type]
        assert [f.name for f in listed] == ["a.pdf"]


async def test_another_bureau_cannot_reach_the_file(db: AsyncSession, tmp_path: Path) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        out = await order_files.upload_file(
            100,
            1000,
            _upload("a.pdf", b"acme"),
            db,
            Staff(1, ACME),
            storage,
            None,  # type: ignore[arg-type]
        )

    with tenant_scope(GLOBEX):
        # Through its own document, or by naming ACME's — neither finds it.
        for order_id, document_id in ((200, 2000), (100, 1000)):
            with pytest.raises(NotFoundError):
                await order_files.download_file(order_id, document_id, out.id, db, storage, None)  # type: ignore[arg-type]
        with pytest.raises(NotFoundError):
            await order_files.delete_file(100, 1000, out.id, db, Staff(2, GLOBEX), None)  # type: ignore[arg-type]


async def test_a_document_of_another_order_is_not_found(db: AsyncSession, tmp_path: Path) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME), pytest.raises(NotFoundError):
        await order_files.upload_file(
            999,
            1000,
            _upload("a.pdf", b"x"),
            db,
            Staff(1, ACME),
            storage,
            None,  # type: ignore[arg-type]
        )


# ── The Order Vault, with translators' working copies ───────────────────────


class FakeVault:
    """The vault's service endpoint: hands out order 41 and counts what it is told."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self.refuse = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path, request.read()))
        if self.refuse:
            return httpx.Response(503)
        if request.method == "POST":
            posts = sum(1 for call in self.calls if call[0] == "POST")
            return httpx.Response(200, json={"orderNo": 41, "fileId": f"{posts:032x}", "size": 1})
        return httpx.Response(200)

    def deletes(self) -> list[str]:
        return [path for method, path, _ in self.calls if method == "DELETE"]


def _vault(vault: FakeVault, working: Path | None) -> VaultStorage:
    return VaultStorage(
        base_url="https://vault.example",
        service_key="k" * 40,
        working=LocalDiskStorage(str(working)) if working else None,
        http=httpx.AsyncClient(transport=httpx.MockTransport(vault)),
    )


async def _close(db: AsyncSession, order_id: int, status: str = "completed") -> None:
    db.add(
        OrderStatusEvent(
            tenant_id=ACME,
            order_id=order_id,
            status=status,
            changed_at=datetime.now(UTC),
        )
    )
    await db.flush()


async def _upload_pair(db: AsyncSession, storage: VaultStorage) -> tuple[Any, Any]:
    staff = Staff(user_id=7, tenant_id=ACME)
    source = await order_files.upload_file(
        100, 1000, _upload("scan.pdf", b"%PDF-1.7 source"), db, staff, storage,
        None,  # type: ignore[arg-type]
        kind=FileKind.SOURCE,
    )  # fmt: skip
    translation = await order_files.upload_file(
        100, 1000, _upload("done.pdf", b"%PDF-1.7 translation"), db, staff, storage,
        None,  # type: ignore[arg-type]
        kind=FileKind.TRANSLATION,
    )  # fmt: skip
    return source, translation


async def test_files_go_to_the_vault_and_the_translator_gets_a_working_copy(
    db: AsyncSession, tmp_path: Path
) -> None:
    """The vault archives the encrypted file; the assigned translator downloads
    a plain working copy; both are made by one upload."""
    vault = FakeVault()
    storage = _vault(vault, tmp_path)

    with tenant_scope(ACME):
        source, translation = await _upload_pair(db, storage)

        # Both are vault files in the same vault order, and both can be downloaded.
        assert (source.in_vault, source.vault_order, source.downloadable) == (True, 41, True)
        assert (translation.in_vault, translation.vault_order) == (True, 41)
        # Only the second upload had to be told which vault order to join.
        assert b'name="order"' not in vault.calls[0][2]
        assert b'name="order"' in vault.calls[1][2]

        # The copy sits in working storage under the tenant/order/document key.
        row_keys = {
            p.name for p in (tmp_path / f"tenants/{ACME}/orders/100/documents/1000").iterdir()
        }
        assert len(row_keys) == 2

        response = await order_files.download_file(100, 1000, source.id, db, storage, None)  # type: ignore[arg-type]
        assert await _body(response) == b"%PDF-1.7 source"


async def test_a_vault_without_working_storage_cannot_be_downloaded_from(
    db: AsyncSession,
) -> None:
    vault = FakeVault()
    storage = _vault(vault, None)
    with tenant_scope(ACME):
        source, _ = await _upload_pair(db, storage)
        assert (source.in_vault, source.downloadable) == (True, False)
        with pytest.raises(StoredInVaultError) as refused:
            await order_files.download_file(100, 1000, source.id, db, storage, None)  # type: ignore[arg-type]
        assert "Order Vault" in refused.value.detail
        assert refused.value.extra == {"vault_order": 41}


async def test_closing_the_order_drops_the_copy_but_not_the_archive(
    db: AsyncSession, tmp_path: Path
) -> None:
    vault = FakeVault()
    storage = _vault(vault, tmp_path)
    cap = datetime.now(UTC) - timedelta(days=60)

    with tenant_scope(ACME):
        source, _ = await _upload_pair(db, storage)

        # Open order: nothing to drop.
        result = await purge_working_copies(db, storage, older_than=cap)
        assert (result.purged, result.failed) == (0, 0)

        await _close(db, 100, "completed")
        result = await purge_working_copies(db, storage, older_than=cap)
        assert (result.purged, result.failed) == (2, 0)
        assert not list((tmp_path / f"tenants/{ACME}/orders/100/documents/1000").iterdir())

        # The file is still listed, now only in the vault, and the vault was not touched.
        listed = await order_files.list_files(100, 1000, db, None)  # type: ignore[arg-type]
        assert [(f.name, f.in_vault, f.downloadable) for f in listed] == [
            ("scan.pdf", True, False),
            ("done.pdf", True, False),
        ]
        assert vault.deletes() == []
        with pytest.raises(StoredInVaultError):
            await order_files.download_file(100, 1000, source.id, db, storage, None)  # type: ignore[arg-type]

        # Running it again is a no-op.
        again = await purge_working_copies(db, storage, older_than=cap)
        assert (again.purged, again.failed) == (0, 0)


@pytest.mark.parametrize("status", ["cancelled", "rejected"])
async def test_cancelled_and_rejected_orders_also_drop_their_copies(
    db: AsyncSession, tmp_path: Path, status: str
) -> None:
    storage = _vault(FakeVault(), tmp_path)
    with tenant_scope(ACME):
        await _upload_pair(db, storage)
        await _close(db, 100, "in_progress")
        await _close(db, 100, status)
        assert (await purge_working_copies(db, storage, older_than=datetime.now(UTC))).purged == 2


async def test_an_order_that_is_reopened_keeps_its_copies(db: AsyncSession, tmp_path: Path) -> None:
    storage = _vault(FakeVault(), tmp_path)
    with tenant_scope(ACME):
        await _upload_pair(db, storage)
        await _close(db, 100, "completed")
        await _close(db, 100, "being translated")  # latest status wins
        cap = datetime.now(UTC) - timedelta(days=60)
        assert (await purge_working_copies(db, storage, older_than=cap)).purged == 0


async def test_a_copy_never_outlives_the_age_cap(db: AsyncSession, tmp_path: Path) -> None:
    storage = _vault(FakeVault(), tmp_path)
    with tenant_scope(ACME):
        await _upload_pair(db, storage)
        # Cap in the future: everything uploaded so far is older than it.
        later = datetime.now(UTC) + timedelta(days=1)
        assert (await purge_working_copies(db, storage, older_than=later)).purged == 2


async def test_a_removed_file_loses_its_copy_then_its_vault_file(
    db: AsyncSession, tmp_path: Path
) -> None:
    vault = FakeVault()
    storage = _vault(vault, tmp_path)
    staff = Staff(user_id=7, tenant_id=ACME)
    with tenant_scope(ACME):
        source, translation = await _upload_pair(db, storage)
        await order_files.delete_file(100, 1000, source.id, db, staff, None)  # type: ignore[arg-type]
        assert [f.name for f in await order_files.list_files(100, 1000, db, None)] == ["done.pdf"]  # type: ignore[arg-type]

        # The copy goes at the next purge even though the order is open ...
        result = await purge_working_copies(
            db, storage, older_than=datetime.now(UTC) - timedelta(days=60)
        )
        assert result.purged == 1
        assert vault.deletes() == []

        # ... and the vault file goes when the retention grace ends.
        result = await purge_removed_files(
            db, storage, cutoff=datetime.now(UTC) + timedelta(days=1)
        )
        assert (result.purged, result.failed) == (1, 0)
        assert vault.deletes() == [f"/api/service/orders/41/files/{1:032x}"]
        # The translation, still live, kept its copy.
        response = await order_files.download_file(100, 1000, translation.id, db, storage, None)  # type: ignore[arg-type]
        assert await _body(response) == b"%PDF-1.7 translation"


async def test_a_failed_vault_upload_leaves_no_copy_behind(
    db: AsyncSession, tmp_path: Path
) -> None:
    vault = FakeVault()
    vault.refuse = True
    storage = _vault(vault, tmp_path)
    with tenant_scope(ACME), pytest.raises(UpstreamUnavailableError):
        await order_files.upload_file(
            100, 1000, _upload("a.pdf", b"%PDF-1.7 x"), db, Staff(1, ACME), storage,
            None,  # type: ignore[arg-type]
        )  # fmt: skip
    leftovers = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert leftovers == []
    assert await order_files.list_files(100, 1000, db, None) == []  # type: ignore[arg-type]


async def test_a_failed_copy_stops_the_upload_before_the_vault_is_touched(
    db: AsyncSession, tmp_path: Path
) -> None:
    vault = FakeVault()
    blocker = tmp_path / "is-a-file"
    blocker.write_text("not a directory")  # a root that cannot hold files
    storage = _vault(vault, blocker)
    with tenant_scope(ACME), pytest.raises(UpstreamUnavailableError):
        await order_files.upload_file(
            100, 1000, _upload("a.pdf", b"%PDF-1.7 x"), db, Staff(1, ACME), storage,
            None,  # type: ignore[arg-type]
        )  # fmt: skip
    assert vault.calls == []


# ── Linking files to documents ──────────────────────────────────────────────


async def _more_documents(db: AsyncSession) -> None:
    """ACME's order 100 gets a second document (English to Russian), and a
    second order of its own gets one, for the files that must not move."""
    with bypass_tenant_scope():
        db.add(
            Order(
                id=101,
                tenant_id=ACME,
                client_id=ACME,
                order_date=date(2026, 10, 1),
                urgency=Urgency.STANDARD,
            )
        )
        await db.flush()
        for document_id, order_id, source, target in (
            (1001, 100, "en", "ru"),
            (1010, 101, "ka", "en"),
        ):
            db.add(
                OrderDocument(
                    id=document_id,
                    tenant_id=ACME,
                    order_id=order_id,
                    document_type_id=ACME,
                    source_language=source,
                    target_language=target,
                    page_count=1,
                    copy_type=CopyType.ORIGINAL,
                    price=Decimal("10"),
                    translator_cost=Decimal("0"),
                    notary_cost=Decimal("0"),
                )
            )
        await db.commit()


async def _scan(
    db: AsyncSession, storage: LocalDiskStorage, name: str, document_id: int = 1000
) -> order_files.StaffFileOut:
    return await order_files.upload_file(
        101 if document_id == 1010 else 100,
        document_id,
        _upload(name, b"%PDF-1.7 " + name.encode()),
        db,
        Staff(7, ACME),
        storage,
        None,  # type: ignore[arg-type]
    )


def _links(*pairs: tuple[str, int]) -> order_files.LinkFilesIn:
    return order_files.LinkFilesIn(
        links=[order_files.FileLink(file_id=file, document_id=to) for file, to in pairs]
    )


async def test_a_file_is_linked_to_another_document_of_the_order(
    db: AsyncSession, tmp_path: Path, audit: list[dict[str, Any]]
) -> None:
    await _more_documents(db)
    storage = LocalDiskStorage(str(tmp_path))
    staff = Staff(7, ACME)

    with tenant_scope(ACME):
        stays = await _scan(db, storage, "stays.pdf")
        moves = await _scan(db, storage, "moves.pdf")
        on_disk = tmp_path / f"tenants/{ACME}/orders/100/documents/1000/{moves.id}"

        linked = await order_files.link_files(
            100,
            # The second is already there: nothing to do, and nothing said about it.
            _links((moves.id, 1001), (stays.id, 1000)),
            db,
            staff,
            None,  # type: ignore[arg-type]
        )

        assert [(f.id, f.document_id) for f in linked] == [(moves.id, 1001)]
        first = await order_files.list_files(100, 1000, db, None)  # type: ignore[arg-type]
        second = await order_files.list_files(100, 1001, db, None)  # type: ignore[arg-type]
        assert [f.id for f in first] == [stays.id]
        assert [f.id for f in second] == [moves.id]

        # The bytes did not move: the row says whose they are now.
        assert on_disk.read_bytes() == b"%PDF-1.7 moves.pdf"
        response = await order_files.download_file(100, 1001, moves.id, db, storage, None)  # type: ignore[arg-type]
        assert await _body(response) == b"%PDF-1.7 moves.pdf"
        # And the old address no longer finds it.
        with pytest.raises(NotFoundError):
            await order_files.download_file(100, 1000, moves.id, db, storage, None)  # type: ignore[arg-type]

        entries = [e for e in audit if e["action"] == "order.files_linked"]
        assert len(entries) == 1
        assert entries[0]["after"]["files"] == [
            {"file_name": "moves.pdf", "from_document_id": 1000, "to_document_id": 1001}
        ]


async def test_linking_files_that_are_already_there_records_nothing(
    db: AsyncSession, tmp_path: Path, audit: list[dict[str, Any]]
) -> None:
    await _more_documents(db)
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        scan = await _scan(db, storage, "a.pdf")
        audit.clear()
        linked = await order_files.link_files(
            100,
            _links((scan.id, 1000)),
            db,
            Staff(7, ACME),
            None,  # type: ignore[arg-type]
        )
        assert linked == []
        assert audit == []


async def test_one_file_that_cannot_move_stops_them_all(db: AsyncSession, tmp_path: Path) -> None:
    await _more_documents(db)
    storage = LocalDiskStorage(str(tmp_path))
    staff = Staff(7, ACME)
    with tenant_scope(ACME):
        mine = await _scan(db, storage, "mine.pdf")
        elsewhere = await _scan(db, storage, "other-order.pdf", document_id=1010)

        # A file of another order of the same bureau is not this order's file.
        with pytest.raises(NotFoundError):
            await order_files.link_files(
                100,
                _links((mine.id, 1001), (elsewhere.id, 1001)),
                db,
                staff,
                None,  # type: ignore[arg-type]
            )
        # Nor is another order's document somewhere to put it.
        with pytest.raises(NotFoundError):
            await order_files.link_files(100, _links((mine.id, 1010)), db, staff, None)  # type: ignore[arg-type]
        listed = await order_files.list_files(100, 1000, db, None)  # type: ignore[arg-type]
        assert [f.id for f in listed] == [mine.id]


async def test_a_removed_file_cannot_be_linked(db: AsyncSession, tmp_path: Path) -> None:
    await _more_documents(db)
    storage = LocalDiskStorage(str(tmp_path))
    staff = Staff(7, ACME)
    with tenant_scope(ACME):
        gone = await _scan(db, storage, "gone.pdf")
        await order_files.delete_file(100, 1000, gone.id, db, staff, None)  # type: ignore[arg-type]
        with pytest.raises(NotFoundError):
            await order_files.link_files(100, _links((gone.id, 1001)), db, staff, None)  # type: ignore[arg-type]


async def test_another_bureau_cannot_link_the_file(db: AsyncSession, tmp_path: Path) -> None:
    await _more_documents(db)
    storage = LocalDiskStorage(str(tmp_path))
    with tenant_scope(ACME):
        scan = await _scan(db, storage, "a.pdf")

    with tenant_scope(GLOBEX):
        for order_id, document_id in ((200, 2000), (100, 1001)):
            with pytest.raises(NotFoundError):
                await order_files.link_files(
                    order_id,
                    _links((scan.id, document_id)),
                    db,
                    Staff(2, GLOBEX),
                    None,  # type: ignore[arg-type]
                )

    with tenant_scope(ACME):
        listed = await order_files.list_files(100, 1000, db, None)  # type: ignore[arg-type]
        assert [f.id for f in listed] == [scan.id]


async def test_a_file_being_translated_stays_where_its_result_will_be_filed(
    db: AsyncSession, tmp_path: Path
) -> None:
    await _more_documents(db)
    storage = LocalDiskStorage(str(tmp_path))
    staff = Staff(7, ACME)
    with tenant_scope(ACME):
        scan = await _scan(db, storage, "scan.pdf")
        row = (
            await db.execute(select(OrderFile).where(OrderFile.public_id == scan.id))
        ).scalar_one()
        job = DocumentTranslation(
            public_id="job-1",
            order_document_id=1000,
            source_file_id=row.id,
            status=TranslationStatus.PROCESSING,
            suliko_job_id="j1",
            suliko_user_id="u1",
            target_language="en",
            page_count=1,
        )
        db.add(job)
        await db.flush()

        links = _links((scan.id, 1001))
        with pytest.raises(ConflictError, match=r"scan\.pdf"):
            await order_files.link_files(100, links, db, staff, None)  # type: ignore[arg-type]

        # Once it has finished, it can.
        job.status = TranslationStatus.COMPLETED
        await db.flush()
        linked = await order_files.link_files(100, links, db, staff, None)  # type: ignore[arg-type]
        assert [f.document_id for f in linked] == [1001]


async def test_a_vault_file_can_be_linked_too(db: AsyncSession) -> None:
    """The vault keeps a file's name and side, not which document it is on."""
    await _more_documents(db)
    with tenant_scope(ACME):
        db.add(
            OrderFile(
                public_id="vault-file-0001",
                order_document_id=1000,
                kind=FileKind.SOURCE,
                file_name="archived.pdf",
                content_type="application/pdf",
                size_bytes=10,
                sha256="0" * 64,
                storage_key="vault/41/vault-file-0001",
                uploaded_by="user:7",
            )
        )
        await db.flush()
        linked = await order_files.link_files(
            100,
            _links(("vault-file-0001", 1001)),
            db,
            Staff(7, ACME),
            None,  # type: ignore[arg-type]
        )
        assert [(f.id, f.document_id) for f in linked] == [("vault-file-0001", 1001)]


def test_a_request_to_link_names_each_file_once() -> None:
    with pytest.raises(ValueError, match="one document at a time"):
        _links(("file-id-0001", 1000), ("file-id-0001", 1001))
    with pytest.raises(ValueError, match="at least 1"):
        order_files.LinkFilesIn(links=[])
