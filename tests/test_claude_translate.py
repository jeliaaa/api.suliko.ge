"""Claude Translate: staff queue files, a separate worker translates them.

Handlers are called directly with a staff stand-in, as ``test_order_files_api.py``
does, against in-memory SQLite and a real ``LocalDiskStorage``. The worker's
code opens its own sessions (``session_scope``), so those are pointed at the same
in-memory database. What SQLite cannot show is two workers racing for one job:
the claim is a single ``UPDATE ... WHERE status = 'queued' RETURNING`` and is
checked here only in that a job, once taken, is not given again.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import UploadFile
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers

from suliko.api.v1 import claude_translate, claude_worker, platform
from suliko.config import get_settings
from suliko.core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain import claude_translate as domain
from suliko.domain.order_files import upload_document_file
from suliko.integrations.object_storage import LocalDiskStorage
from suliko.models.claude_translation import (
    ClaudeJobStatus,
    ClaudeTranslationJob,
    ClaudeWorkerHeartbeat,
)
from suliko.models.directory import Client, ClientType
from suliko.models.order import CopyType, Order, OrderDocument, OrderStatusEvent, Urgency
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind
from suliko.models.reference import DocumentType, Language
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import User

ACME, GLOBEX = 1, 2
TOKEN = "worker-token-for-tests"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


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


@pytest.fixture(autouse=True)
def _worker_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "claude_worker_tokens", SecretStr(f"old-token,{TOKEN}"))
    monkeypatch.setattr(settings, "claude_worker_allowed_ips", "")


@pytest_asyncio.fixture
async def maker(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    tables = [
        m.__table__
        for m in (
            Tenant,
            User,
            Client,
            DocumentType,
            Language,
            Order,
            OrderDocument,
            OrderFile,
            OrderStatusEvent,
            ClaudeTranslationJob,
            ClaudeWorkerHeartbeat,
        )
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    session_maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with session_maker() as session:
            yield session
            await session.commit()

    # The worker's code opens its own sessions: point them at this database.
    monkeypatch.setattr(domain, "session_scope", scope)
    monkeypatch.setattr(claude_worker, "session_scope", scope)

    async with session_maker() as session:
        with bypass_tenant_scope():
            for tenant_id, slug in ((ACME, "acme"), (GLOBEX, "globex")):
                session.add(
                    Tenant(
                        id=tenant_id,
                        slug=slug,
                        display_name=slug,
                        status=TenantStatus.ACTIVE,
                        locale="ka",
                        claude_translate_enabled=True,
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
                        Language(
                            tenant_id=tenant_id, code="ka", name_en="Georgian", name_ka="ქართული"
                        ),
                        Language(
                            tenant_id=tenant_id, code="ru", name_en="Russian", name_ka="რუსული"
                        ),
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
                        translation_instructions="Spell Tamar as Tamara."
                        if tenant_id == ACME
                        else None,
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


@pytest_asyncio.fixture
async def db(maker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with maker() as session:
        yield session


def _docx(text: str = "hello") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", f"<w:document>{text}</w:document>")
    return buffer.getvalue()


def _upload(name: str, content: bytes, content_type: str = DOCX) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content), filename=name, headers=Headers({"content-type": content_type})
    )


async def _add_file(
    db: AsyncSession,
    storage: LocalDiskStorage,
    tenant_id: int,
    name: str,
    kind: FileKind = FileKind.SOURCE,
) -> OrderFile:
    with tenant_scope(tenant_id):
        order = await db.get(Order, tenant_id * 100)
        document = await db.get(OrderDocument, tenant_id * 1000)
        assert order and document
        row = await upload_document_file(
            db,
            storage,
            order=order,
            document=document,
            kind=kind,
            file_name=name,
            content=b"%PDF-1.7 " + name.encode(),
            content_type="application/pdf",
            uploaded_by="user:1",
        )
        await db.commit()
        return row


async def _queue(
    db: AsyncSession, staff: Staff, order_id: int, files: list[str], **kwargs: Any
) -> list[claude_translate.JobOut]:
    with tenant_scope(staff.tenant_id):
        return await claude_translate.create_jobs(
            order_id,
            claude_translate.CreateJobsIn(files=files, **kwargs),
            db,
            staff,  # type: ignore[arg-type]
            None,
            None,
        )


ACME_STAFF = Staff(user_id=7, tenant_id=ACME)


# ── Staff: the switch ────────────────────────────────────────────────────────


async def test_an_organisation_without_the_switch_is_refused(db: AsyncSession) -> None:
    with bypass_tenant_scope():
        tenant = await db.get(Tenant, ACME)
        assert tenant
        tenant.claude_translate_enabled = False
        await db.commit()
    with pytest.raises(claude_translate.ClaudeTranslateNotEnabledError) as caught:
        await claude_translate.feature_enabled(db, ACME_STAFF)  # type: ignore[arg-type]
    assert caught.value.error_code == "claude_translate_not_enabled"


async def test_an_organisation_with_the_switch_passes(db: AsyncSession) -> None:
    assert await claude_translate.feature_enabled(db, ACME_STAFF) is None  # type: ignore[arg-type]


# ── Staff: picking files ─────────────────────────────────────────────────────


async def test_only_ticked_source_files_become_jobs(
    db: AsyncSession, tmp_path: Path, audit: list[dict[str, Any]]
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    one = await _add_file(db, storage, ACME, "one.pdf")
    await _add_file(db, storage, ACME, "two.pdf")
    three = await _add_file(db, storage, ACME, "three.pdf")
    await _add_file(db, storage, ACME, "already.docx", kind=FileKind.TRANSLATION)

    with tenant_scope(ACME):
        listed = await claude_translate.list_pickable_files(100, db, None, None)  # type: ignore[arg-type]
    assert {f.name for f in listed.files} == {"one.pdf", "two.pdf", "three.pdf"}
    assert listed.instructions == "Spell Tamar as Tamara."
    assert {(f.source_language, f.target_language) for f in listed.files} == {("ru", "ka")}

    jobs = await _queue(db, ACME_STAFF, 100, [one.public_id, three.public_id])

    assert sorted(j.file_name for j in jobs) == ["one.pdf", "three.pdf"]
    assert {j.status for j in jobs} == {ClaudeJobStatus.QUEUED}
    assert audit[-1]["action"] == "order.claude_translation_requested"
    with tenant_scope(ACME):
        rows = (await db.execute(select(ClaudeTranslationJob))).scalars().all()
    assert len(rows) == 2
    # What the worker will be told is fixed at the click.
    assert {r.instructions for r in rows} == {"Spell Tamar as Tamara."}
    assert {(r.source_language, r.target_language) for r in rows} == {("ru", "ka")}
    assert {r.requested_by_user_id for r in rows} == {7}


async def test_a_file_of_another_order_is_refused_and_nothing_is_queued(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    mine = await _add_file(db, storage, ACME, "mine.pdf")
    theirs = await _add_file(db, storage, GLOBEX, "theirs.pdf")

    with pytest.raises(NotFoundError):
        await _queue(db, ACME_STAFF, 100, [mine.public_id, theirs.public_id])
    with pytest.raises(NotFoundError):
        await _queue(db, ACME_STAFF, 200, [mine.public_id])
    with tenant_scope(ACME):
        assert (await db.execute(select(ClaudeTranslationJob))).scalars().all() == []


async def test_a_translation_file_cannot_be_picked(db: AsyncSession, tmp_path: Path) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    done = await _add_file(db, storage, ACME, "done.docx", kind=FileKind.TRANSLATION)
    with pytest.raises(NotFoundError):
        await _queue(db, ACME_STAFF, 100, [done.public_id])


async def test_a_removed_file_cannot_be_picked(db: AsyncSession, tmp_path: Path) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    gone = await _add_file(db, storage, ACME, "gone.pdf")
    gone.deleted_at = datetime.now(UTC)
    await db.commit()
    with pytest.raises(NotFoundError):
        await _queue(db, ACME_STAFF, 100, [gone.public_id])


async def test_a_second_click_on_an_active_file_is_refused(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    one = await _add_file(db, storage, ACME, "one.pdf")
    await _queue(db, ACME_STAFF, 100, [one.public_id])
    with pytest.raises(claude_translate.JobActiveError) as caught:
        await _queue(db, ACME_STAFF, 100, [one.public_id])
    assert caught.value.extra["files"] == ["one.pdf"]


async def test_the_database_itself_refuses_two_active_jobs_for_a_file(
    db: AsyncSession, tmp_path: Path
) -> None:
    """Two clicks at once both pass the check; the unique index stops the second."""
    from sqlalchemy.exc import IntegrityError

    storage = LocalDiskStorage(str(tmp_path))
    one = await _add_file(db, storage, ACME, "one.pdf")
    await _queue(db, ACME_STAFF, 100, [one.public_id])
    with tenant_scope(ACME):
        db.add(
            ClaudeTranslationJob(
                public_id="x" * 22,
                order_id=100,
                order_document_id=1000,
                source_file_id=one.id,
                status=ClaudeJobStatus.QUEUED,
                file_name="one.pdf",
                content_type="application/pdf",
                source_language="ru",
                target_language="ka",
                attempts=0,
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()
    await db.rollback()


# ── Staff: statuses, retry, cancel ───────────────────────────────────────────


async def test_retry_and_cancel_follow_the_rules(db: AsyncSession, tmp_path: Path) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    one = await _add_file(db, storage, ACME, "one.pdf")
    (job,) = await _queue(db, ACME_STAFF, 100, [one.public_id])

    with tenant_scope(ACME):
        # Waiting: it can be cancelled but not retried.
        with pytest.raises(ConflictError):
            await claude_translate.retry_job(100, job.id, db, ACME_STAFF, None, None)  # type: ignore[arg-type]

        row = (await db.execute(select(ClaudeTranslationJob))).scalar_one()
        row.status = ClaudeJobStatus.TRANSLATING
        await db.flush()
        # Held by the worker: not cancellable.
        with pytest.raises(ConflictError):
            await claude_translate.cancel_job(100, job.id, db, ACME_STAFF, None, None)  # type: ignore[arg-type]

        row.status = ClaudeJobStatus.FAILED
        row.error = "usage limit"
        row.attempts = 3
        await db.flush()
        retried = await claude_translate.retry_job(100, job.id, db, ACME_STAFF, None, None)  # type: ignore[arg-type]
        assert retried.status is ClaudeJobStatus.QUEUED
        assert retried.attempts == 0
        assert retried.error is None

        cancelled = await claude_translate.cancel_job(100, job.id, db, ACME_STAFF, None, None)  # type: ignore[arg-type]
        assert cancelled.status is ClaudeJobStatus.CANCELLED
        assert cancelled.finished_at is not None


async def test_a_job_of_another_organisation_is_not_found(db: AsyncSession, tmp_path: Path) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    theirs = await _add_file(db, storage, GLOBEX, "theirs.pdf")
    (job,) = await _queue(db, Staff(user_id=9, tenant_id=GLOBEX), 200, [theirs.public_id])
    with tenant_scope(ACME):
        with pytest.raises(NotFoundError):
            await claude_translate.cancel_job(100, job.id, db, ACME_STAFF, None, None)  # type: ignore[arg-type]
        with pytest.raises(NotFoundError):
            await claude_translate.cancel_job(200, job.id, db, ACME_STAFF, None, None)  # type: ignore[arg-type]


# ── The worker: who may call ─────────────────────────────────────────────────


class FakeRequest:
    def __init__(self, token: str | None, forwarded: str | None = None, peer: str = "10.0.0.5"):
        self.headers = {}
        if token is not None:
            self.headers["authorization"] = f"Bearer {token}"
        if forwarded:
            self.headers["x-forwarded-for"] = forwarded
        self.client = type("C", (), {"host": peer})()
        self.url = type("U", (), {"path": "/api/v1/worker/jobs/claim"})()


async def test_a_wrong_or_missing_token_is_refused() -> None:
    for token in (None, "", "nope", TOKEN[:-1]):
        with pytest.raises(AuthenticationError):
            await claude_worker.require_worker(FakeRequest(token))  # type: ignore[arg-type]


async def test_either_configured_token_is_accepted_so_one_can_be_rotated() -> None:
    assert await claude_worker.require_worker(FakeRequest(TOKEN), "box-1") == "box-1"  # type: ignore[arg-type]
    assert await claude_worker.require_worker(FakeRequest("old-token")) == "default"  # type: ignore[arg-type]


async def test_no_tokens_configured_switches_the_api_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "claude_worker_tokens", SecretStr(""))
    with pytest.raises(AuthenticationError):
        await claude_worker.require_worker(FakeRequest(""))  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError):
        await claude_worker.require_worker(FakeRequest(TOKEN))  # type: ignore[arg-type]


async def test_the_address_list_uses_the_last_forwarded_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "claude_worker_allowed_ips", "203.0.113.7, 198.51.100.0/24")
    ok = FakeRequest(TOKEN, forwarded="1.2.3.4, 203.0.113.7")
    assert await claude_worker.require_worker(ok) == "default"  # type: ignore[arg-type]
    in_range = FakeRequest(TOKEN, forwarded="198.51.100.99:5555")
    assert await claude_worker.require_worker(in_range) == "default"  # type: ignore[arg-type]
    # The first hop is whatever the caller wrote: claiming an allowed address
    # in front of the real one does nothing.
    spoofed = FakeRequest(TOKEN, forwarded="203.0.113.7, 9.9.9.9")
    with pytest.raises(AuthenticationError):
        await claude_worker.require_worker(spoofed)  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError):
        await claude_worker.require_worker(FakeRequest(TOKEN, peer="10.0.0.5"))  # type: ignore[arg-type]


def test_the_gateway_lets_the_worker_through_to_its_own_check() -> None:
    from fastapi.testclient import TestClient

    from suliko.main import create_app

    settings = get_settings()
    original = settings.bff_shared_secret
    object.__setattr__(settings, "bff_shared_secret", SecretStr("gateway-secret-for-tests"))
    try:
        with TestClient(create_app()) as client:
            # Not 404, which is what the gateway answers a stranger: the worker
            # router's own 401.
            assert client.post("/api/v1/worker/jobs/claim").status_code == 401
            assert client.get("/api/v1/clients").status_code == 404
    finally:
        object.__setattr__(settings, "bff_shared_secret", original)


# ── The worker: claiming ─────────────────────────────────────────────────────


async def test_claim_gives_the_oldest_job_once(
    db: AsyncSession, maker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    one = await _add_file(db, storage, ACME, "ASUS_ISO14001.pdf")
    two = await _add_file(db, storage, ACME, "two.pdf")
    await _queue(db, ACME_STAFF, 100, [one.public_id])
    await _queue(db, ACME_STAFF, 100, [two.public_id])

    first = await claude_worker.claim("w1")
    assert isinstance(first, claude_worker.ClaimedOut)
    assert first.file_name == "ASUS_ISO14001.pdf"
    assert first.output_file_name == "ASUS_ISO14001_Translated.docx"
    assert (first.source_language, first.target_language) == ("ru", "ka")
    assert (first.source_language_name, first.target_language_name) == ("Russian", "Georgian")
    assert first.instructions == "Spell Tamar as Tamara."
    assert first.attempts == 1
    assert first.order_number >= 0
    assert first.job_id.startswith(f"{ACME}.")
    assert first.source_path.endswith(f"/worker/jobs/{first.job_id}/source")

    second = await claude_worker.claim("w1")
    assert isinstance(second, claude_worker.ClaimedOut)
    assert second.file_name == "two.pdf"
    assert second.job_id != first.job_id

    nothing = await claude_worker.claim("w1")
    assert nothing.status_code == 204  # type: ignore[union-attr]

    async with maker() as check:
        beat = (await check.execute(select(ClaudeWorkerHeartbeat))).scalar_one()
        assert beat.name == "w1"
        with tenant_scope(ACME):
            states = {
                r.file_name: (r.status, r.attempts)
                for r in (await check.execute(select(ClaudeTranslationJob))).scalars()
            }
    assert states == {
        "ASUS_ISO14001.pdf": (ClaudeJobStatus.TRANSLATING, 1),
        "two.pdf": (ClaudeJobStatus.TRANSLATING, 1),
    }


async def test_claim_serves_the_organisation_with_the_oldest_job_and_skips_switched_off_ones(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    acme_file = await _add_file(db, storage, ACME, "acme.pdf")
    globex_file = await _add_file(db, storage, GLOBEX, "globex.pdf")
    await _queue(db, Staff(user_id=9, tenant_id=GLOBEX), 200, [globex_file.public_id])
    await _queue(db, ACME_STAFF, 100, [acme_file.public_id])

    with bypass_tenant_scope():
        globex = await db.get(Tenant, GLOBEX)
        assert globex
        globex.claude_translate_enabled = False
        await db.commit()

    got = await claude_worker.claim("w1")
    assert isinstance(got, claude_worker.ClaimedOut)
    assert got.file_name == "acme.pdf"
    assert (await claude_worker.claim("w1")).status_code == 204  # type: ignore[union-attr]

    with bypass_tenant_scope():
        globex.claude_translate_enabled = True
        await db.commit()
    again = await claude_worker.claim("w1")
    assert isinstance(again, claude_worker.ClaimedOut)
    assert again.file_name == "globex.pdf"


async def test_a_job_whose_file_was_removed_while_waiting_is_failed_not_handed_out(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    gone = await _add_file(db, storage, ACME, "gone.pdf")
    kept = await _add_file(db, storage, ACME, "kept.pdf")
    await _queue(db, ACME_STAFF, 100, [gone.public_id])
    await _queue(db, ACME_STAFF, 100, [kept.public_id])
    gone.deleted_at = datetime.now(UTC)
    await db.commit()

    got = await claude_worker.claim("w1")
    assert isinstance(got, claude_worker.ClaimedOut)
    assert got.file_name == "kept.pdf"
    with tenant_scope(ACME):
        failed = (
            await db.execute(
                select(ClaudeTranslationJob).where(ClaudeTranslationJob.file_name == "gone.pdf")
            )
        ).scalar_one()
    assert failed.status is ClaudeJobStatus.FAILED
    assert failed.error == domain.FILE_GONE_MESSAGE


# ── The worker: downloading, reporting ───────────────────────────────────────


async def _claimed(db: AsyncSession, tmp_path: Path, name: str = "scan.pdf") -> str:
    storage = LocalDiskStorage(str(tmp_path))
    row = await _add_file(db, storage, ACME, name)
    await _queue(db, ACME_STAFF, 100, [row.public_id])
    got = await claude_worker.claim("w1")
    assert isinstance(got, claude_worker.ClaimedOut)
    return got.job_id


async def test_the_source_downloads_only_while_the_job_is_the_workers(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    job_id = await _claimed(db, tmp_path)
    response = await claude_worker.download_source(job_id, storage, "w1")
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert body == b"%PDF-1.7 scan.pdf"
    assert response.headers["content-disposition"].startswith("attachment;")

    await claude_worker.report_status(
        job_id, claude_worker.StatusIn(status="failed", error_message="x"), "w1"
    )
    with pytest.raises(ConflictError):
        await claude_worker.download_source(job_id, storage, "w1")


async def test_a_malformed_or_foreign_job_reference_is_not_found(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    for ref in ("nonsense", "1.", "../1.abc", "999.abcdefghij", f"{ACME}.abcdefghij"):
        with pytest.raises(NotFoundError):
            await claude_worker.download_source(ref, storage, "w1")
    # A reference from one organisation never opens another's job.
    job_id = await _claimed(db, tmp_path)
    _, public = job_id.split(".", 1)
    with pytest.raises(NotFoundError):
        await claude_worker.download_source(f"{GLOBEX}.{public}", storage, "w1")


async def test_the_result_is_filed_under_the_document_and_the_job_is_translated(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    job_id = await _claimed(db, tmp_path, "ASUS_ISO14001.pdf")

    out = await claude_worker.post_result(job_id, _upload("whatever.docx", _docx()), storage, "w1")
    assert out.output_file_name == "ASUS_ISO14001_Translated.docx"

    with tenant_scope(ACME):
        job = (await db.execute(select(ClaudeTranslationJob))).scalar_one()
        assert job.status is ClaudeJobStatus.TRANSLATED
        assert job.finished_at is not None
        result = await db.get(OrderFile, job.result_file_id)
    assert result is not None
    assert result.kind is FileKind.TRANSLATION
    assert result.file_name == "ASUS_ISO14001_Translated.docx"
    assert result.content_type == DOCX
    assert result.order_document_id == 1000
    assert result.uploaded_by == "claude:7"
    on_disk = tmp_path / f"tenants/{ACME}/orders/100/documents/1000/{result.public_id}"
    assert on_disk.read_bytes() == _docx()

    # A second upload for the same job finds it done.
    with pytest.raises(ConflictError):
        await claude_worker.post_result(job_id, _upload("again.docx", _docx()), storage, "w1")


async def test_translating_again_adds_a_numbered_file_and_replaces_nothing(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    first = await _claimed(db, tmp_path, "report.pdf")
    await claude_worker.post_result(first, _upload("a.docx", _docx("one")), storage, "w1")

    with tenant_scope(ACME):
        source = (
            await db.execute(select(OrderFile).where(OrderFile.file_name == "report.pdf"))
        ).scalar_one()
    # Said yes to translating it again.
    with pytest.raises(claude_translate.RetranslateNeedsConfirmationError):
        await _queue(db, ACME_STAFF, 100, [source.public_id])
    await _queue(db, ACME_STAFF, 100, [source.public_id], confirm_retranslate=True)
    got = await claude_worker.claim("w1")
    assert isinstance(got, claude_worker.ClaimedOut)
    out = await claude_worker.post_result(
        got.job_id, _upload("b.docx", _docx("two")), storage, "w1"
    )

    assert out.output_file_name == "report_Translated (2).docx"
    with tenant_scope(ACME):
        names = sorted(
            (
                await db.execute(
                    select(OrderFile.file_name).where(OrderFile.kind == FileKind.TRANSLATION)
                )
            )
            .scalars()
            .all()
        )
    assert names == ["report_Translated (2).docx", "report_Translated.docx"]


async def test_bytes_that_are_not_a_word_document_are_refused_and_the_job_stays_the_workers(
    db: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    job_id = await _claimed(db, tmp_path)
    with pytest.raises(ValidationError):
        await claude_worker.post_result(job_id, _upload("x.docx", b"not a zip"), storage, "w1")
    with tenant_scope(ACME):
        job = (await db.execute(select(ClaudeTranslationJob))).scalar_one()
    assert job.status is ClaudeJobStatus.TRANSLATING


async def test_a_failure_report_shows_its_message_and_can_be_retried(
    db: AsyncSession, tmp_path: Path
) -> None:
    job_id = await _claimed(db, tmp_path)
    out = await claude_worker.report_status(
        job_id, claude_worker.StatusIn(status="failed", error_message="file is encrypted"), "w1"
    )
    assert out.status is ClaudeJobStatus.FAILED
    with tenant_scope(ACME):
        listed = await claude_translate.list_jobs(100, db, None, None)  # type: ignore[arg-type]
        (job,) = listed.jobs
        assert (job.status, job.error) == (ClaudeJobStatus.FAILED, "file is encrypted")
        assert listed.worker_online is True
        retried = await claude_translate.retry_job(100, job.id, db, ACME_STAFF, None, None)  # type: ignore[arg-type]
    assert retried.status is ClaudeJobStatus.QUEUED
    # A report about a job that is not the worker's is refused.
    with pytest.raises(ConflictError):
        await claude_worker.report_status(
            job_id, claude_worker.StatusIn(status="failed", error_message="again"), "w1"
        )


async def test_try_later_returns_the_job_and_gives_the_attempt_back(
    db: AsyncSession, tmp_path: Path
) -> None:
    job_id = await _claimed(db, tmp_path)
    await claude_worker.report_status(
        job_id,
        claude_worker.StatusIn(status="queued", error_message="usage limit, retry later"),
        "w1",
    )
    with tenant_scope(ACME):
        job = (await db.execute(select(ClaudeTranslationJob))).scalar_one()
    assert job.status is ClaudeJobStatus.QUEUED
    assert job.attempts == 0
    assert job.error == "usage limit, retry later"
    again = await claude_worker.claim("w1")
    assert isinstance(again, claude_worker.ClaimedOut)
    assert again.attempts == 1


def test_a_success_cannot_be_reported_without_a_result() -> None:
    with pytest.raises(ValueError):
        claude_worker.StatusIn(status="translated")  # type: ignore[arg-type]


# ── Stuck jobs ───────────────────────────────────────────────────────────────


async def test_a_job_held_over_thirty_minutes_goes_back_and_is_failed_after_three_tries(
    db: AsyncSession, tmp_path: Path
) -> None:
    job_id = await _claimed(db, tmp_path)
    with tenant_scope(ACME):
        job = (await db.execute(select(ClaudeTranslationJob))).scalar_one()
        job.started_at = datetime.now(UTC) - timedelta(minutes=29)
        await db.commit()
        assert await domain.requeue_stuck(db) == 0

        job.started_at = datetime.now(UTC) - timedelta(minutes=31)
        await db.commit()
        assert await domain.requeue_stuck(db) == 1
        await db.refresh(job)
        assert job.status is ClaudeJobStatus.QUEUED
        assert job.error == domain.STUCK_MESSAGE
        assert job.attempts == 1

        job.status = ClaudeJobStatus.TRANSLATING
        job.attempts = 3
        job.started_at = datetime.now(UTC) - timedelta(minutes=45)
        await db.commit()
        assert await domain.requeue_stuck(db) == 1
        await db.refresh(job)
        assert job.status is ClaudeJobStatus.FAILED
        assert job.finished_at is not None
        assert "3 times" in (job.error or "")
    assert job_id.startswith(f"{ACME}.")


# ── The platform's switch ────────────────────────────────────────────────────


async def test_a_superuser_switches_an_organisation_on_and_off(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    session = Staff(user_id=1, tenant_id=ACME)
    off = await platform.set_claude_translate(
        GLOBEX,
        platform.ClaudeTranslateChange(enabled=False),
        db,
        session,  # type: ignore[arg-type]
    )
    assert off.claude_translate is False
    assert audit[-1]["action"] == "platform.claude_translate_changed"
    assert audit[-1]["before"] == {"enabled": True}

    count = len(audit)
    again = await platform.set_claude_translate(
        GLOBEX,
        platform.ClaudeTranslateChange(enabled=False),
        db,
        session,  # type: ignore[arg-type]
    )
    assert again.claude_translate is False
    assert len(audit) == count  # nothing changed, nothing to audit

    on = await platform.set_claude_translate(
        GLOBEX,
        platform.ClaudeTranslateChange(enabled=True),
        db,
        session,  # type: ignore[arg-type]
    )
    assert on.claude_translate is True
    with pytest.raises(NotFoundError):
        await platform.set_claude_translate(
            404,
            platform.ClaudeTranslateChange(enabled=True),
            db,
            session,  # type: ignore[arg-type]
        )


def test_the_names_of_results_and_a_taken_name() -> None:
    assert domain.result_name("ASUS_ISO14001.pdf") == "ASUS_ISO14001_Translated.docx"
    assert domain.result_name("no extension") == "no extension_Translated.docx"
    assert domain.result_name("a.b.c.jpg") == "a.b.c_Translated.docx"
    assert domain.unique_name("x.docx", {"y.docx"}) == "x.docx"
    assert domain.unique_name("x.docx", {"X.DOCX", "x (2).docx"}) == "x (3).docx"


def test_worker_online_means_seen_within_five_minutes() -> None:
    now = datetime.now(UTC)
    assert domain.worker_online(now - timedelta(minutes=4), now=now) is True
    assert domain.worker_online(now - timedelta(minutes=6), now=now) is False
    assert domain.worker_online(None) is False
