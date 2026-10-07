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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from suliko.api.v1 import order_files
from suliko.api.v1.order_files import StoredInVaultError
from suliko.core.errors import NotFoundError, UpstreamUnavailableError
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
        for m in (Tenant, Client, DocumentType, Order, OrderDocument, OrderFile, OrderStatusEvent)
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
