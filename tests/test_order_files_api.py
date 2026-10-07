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
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from suliko.api.v1 import order_files
from suliko.core.errors import NotFoundError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.integrations.object_storage import LocalDiskStorage
from suliko.models.directory import Client, ClientType
from suliko.models.order import CopyType, Order, OrderDocument, Urgency
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
    tables = [m.__table__ for m in (Tenant, Client, DocumentType, Order, OrderDocument, OrderFile)]
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
