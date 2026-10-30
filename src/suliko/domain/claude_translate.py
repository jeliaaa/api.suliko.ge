"""Claude Translate: the queue and its rules.

Two callers share this. Staff of an organisation that has the feature queue
files, look at them, retry and cancel (``api/v1/claude_translate.py``). The
Claude Code server claims jobs, reports and files results
(``api/v1/claude_worker.py``). Neither passes a tenant id the other could
forge: staff are inside their own tenant's scope, and the worker is given a job
reference that names the tenant, which only a worker holding the token can
present.

## One job, one state

    queued -> translating -> translated
       |           |
       |           +-> failed       (the worker said so, or it was held too often)
       |           +-> queued       (the worker asked to try later, or it went silent)
       +-> cancelled                (staff, only while it waits)
    failed -> queued                (staff retry)

Every move is an ``UPDATE ... WHERE status = <the state it is moving from>``
with ``RETURNING``, so two requests racing for one job cannot both win.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.config import get_settings
from suliko.core.errors import NotFoundError
from suliko.db.session import session_scope
from suliko.db.tenancy import bypass_tenant_scope, tenant_scope
from suliko.models.claude_translation import (
    ClaudeJobStatus,
    ClaudeTranslationJob,
    ClaudeWorkerHeartbeat,
)
from suliko.models.order import Order
from suliko.models.order_file import OrderFile
from suliko.models.reference import Language
from suliko.models.tenant import Tenant, TenantStatus

log = structlog.get_logger()

#: ``<tenant id>.<public id>``: what the worker calls a job.
_REF = re.compile(r"^(\d{1,12})\.([A-Za-z0-9_-]{8,40})$")

#: Shown to the person, not the worker, when a job is put back or given up.
STUCK_MESSAGE = "The translation did not finish in time."
FILE_GONE_MESSAGE = "The file was removed before it could be translated."


def job_ref(job: ClaudeTranslationJob) -> str:
    return f"{job.tenant_id}.{job.public_id}"


def parse_job_ref(ref: str) -> tuple[int, str]:
    match = _REF.fullmatch(ref)
    if match is None:
        raise NotFoundError("Job not found.")
    return int(match.group(1)), match.group(2)


def result_name(source_name: str) -> str:
    """``ASUS_ISO14001.pdf`` becomes ``ASUS_ISO14001_Translated.docx``."""
    stem = source_name.rsplit(".", 1)[0] if "." in source_name else source_name
    return f"{(stem or 'translation')[:180]}_Translated.docx"


def unique_name(name: str, taken: set[str]) -> str:
    """``name`` with a number before the extension if the document already has a
    file called that, so a second translation does not hide the first."""
    if name.lower() not in {t.lower() for t in taken}:
        return name
    stem, dot, extension = name.rpartition(".")
    for number in range(2, 1000):
        candidate = f"{stem} ({number}){dot}{extension}"
        if candidate.lower() not in {t.lower() for t in taken}:
            return candidate
    return name


# ── Putting jobs back ────────────────────────────────────────────────────────


async def requeue_stuck(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Return jobs the worker has held too long to the queue; fail the ones that
    have had their attempts. Inside a tenant scope. Returns how many it moved.

    Run whenever the queue is looked at (a claim, the order's list) rather than
    by a timer of its own: if nobody is looking, nobody is waiting on it.
    """
    settings = get_settings()
    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=settings.claude_job_stuck_minutes)
    held = (
        ClaudeTranslationJob.status == ClaudeJobStatus.TRANSLATING,
        ClaudeTranslationJob.started_at < cutoff,
    )
    gave_up = await db.execute(
        update(ClaudeTranslationJob)
        .where(*held, ClaudeTranslationJob.attempts >= settings.claude_job_max_attempts)
        .values(
            status=ClaudeJobStatus.FAILED,
            error=f"{STUCK_MESSAGE} It was tried {settings.claude_job_max_attempts} times.",
            finished_at=now or datetime.now(UTC),
        )
        .returning(ClaudeTranslationJob.id)
    )
    put_back = await db.execute(
        update(ClaudeTranslationJob)
        .where(*held, ClaudeTranslationJob.attempts < settings.claude_job_max_attempts)
        .values(status=ClaudeJobStatus.QUEUED, error=STUCK_MESSAGE)
        .returning(ClaudeTranslationJob.id)
    )
    moved = len(gave_up.all()) + len(put_back.all())
    if moved:
        log.info("claude_jobs_requeued", count=moved)
    return moved


# ── The worker's side ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Claimed:
    """What the worker is told about a job it has just been given."""

    ref: str
    order_number: int
    file_name: str
    content_type: str
    size_bytes: int
    source_language: str
    target_language: str
    source_language_name: str
    target_language_name: str
    instructions: str | None
    attempts: int
    output_name: str


async def _language_name(db: AsyncSession, code: str) -> str:
    row = (await db.execute(select(Language).where(Language.code == code))).scalars().first()
    return (row.name_en if row and row.name_en else None) or code


async def _claim_in_tenant(db: AsyncSession, tenant_id: int) -> Claimed | None:
    """Take the tenant's oldest waiting job, or None. Inside the tenant's scope."""
    while True:
        waiting = (
            await db.execute(
                select(ClaudeTranslationJob.id)
                .where(ClaudeTranslationJob.status == ClaudeJobStatus.QUEUED)
                .order_by(ClaudeTranslationJob.created_at, ClaudeTranslationJob.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if waiting is None:
            return None
        won = (
            await db.execute(
                update(ClaudeTranslationJob)
                .where(
                    ClaudeTranslationJob.id == waiting,
                    ClaudeTranslationJob.tenant_id == tenant_id,
                    ClaudeTranslationJob.status == ClaudeJobStatus.QUEUED,
                )
                .values(
                    status=ClaudeJobStatus.TRANSLATING,
                    started_at=datetime.now(UTC),
                    attempts=ClaudeTranslationJob.attempts + 1,
                    error=None,
                )
                .returning(ClaudeTranslationJob.id)
            )
        ).first()
        if won is None:
            continue
        job = await db.get(ClaudeTranslationJob, waiting, populate_existing=True)
        if job is None:  # pragma: no cover - just updated
            continue
        source = await db.get(OrderFile, job.source_file_id) if job.source_file_id else None
        if source is None or source.deleted_at is not None:
            # Removed while it waited: nothing to translate, and nobody to ask.
            job.status = ClaudeJobStatus.FAILED
            job.error = FILE_GONE_MESSAGE
            job.finished_at = datetime.now(UTC)
            await db.flush()
            continue
        order = await db.get(Order, job.order_id)
        return Claimed(
            ref=job_ref(job),
            order_number=order.number if order else 0,
            file_name=job.file_name,
            content_type=job.content_type,
            size_bytes=source.size_bytes,
            source_language=job.source_language,
            target_language=job.target_language,
            source_language_name=await _language_name(db, job.source_language),
            target_language_name=await _language_name(db, job.target_language),
            instructions=job.instructions,
            attempts=job.attempts,
            output_name=result_name(job.file_name),
        )


async def claim_next() -> Claimed | None:
    """The oldest waiting job of any organisation that has the feature.

    Each organisation is entered on its own, as ``suliko purge-files`` does, so
    row-level security applies exactly as it does to a request. The oldest
    across all of them goes first; if another worker takes it in the meantime
    the next oldest is tried.
    """
    with bypass_tenant_scope():
        async with session_scope() as db:
            tenants = [
                int(i)
                for i in (
                    await db.execute(
                        select(Tenant.id)
                        .where(
                            Tenant.claude_translate_enabled.is_(True),
                            Tenant.status.in_((TenantStatus.ACTIVE, TenantStatus.TRIAL)),
                        )
                        .order_by(Tenant.id)
                    )
                )
                .scalars()
                .all()
            ]

    waiting: list[tuple[datetime, int]] = []
    for tenant_id in tenants:
        with tenant_scope(tenant_id):
            async with session_scope() as db:
                await requeue_stuck(db)
                oldest = (
                    await db.execute(
                        select(ClaudeTranslationJob.created_at)
                        .where(ClaudeTranslationJob.status == ClaudeJobStatus.QUEUED)
                        .order_by(ClaudeTranslationJob.created_at)
                        .limit(1)
                    )
                ).scalar_one_or_none()
        if oldest is not None:
            waiting.append((oldest, tenant_id))

    for _, tenant_id in sorted(waiting):
        with tenant_scope(tenant_id):
            async with session_scope() as db:
                claimed = await _claim_in_tenant(db, tenant_id)
        if claimed is not None:
            log.info("claude_job_claimed", job=claimed.ref, attempts=claimed.attempts)
            return claimed
    return None


async def job_in_tenant(db: AsyncSession, ref: str) -> ClaudeTranslationJob:
    """The job a worker names. The tenant scope must already be the ref's."""
    tenant_id, public_id = parse_job_ref(ref)
    job = (
        await db.execute(
            select(ClaudeTranslationJob).where(
                ClaudeTranslationJob.tenant_id == tenant_id,
                ClaudeTranslationJob.public_id == public_id,
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundError("Job not found.")
    return job


async def enabled_tenant(tenant_id: int) -> bool:
    """Whether the organisation still has the feature and may be served."""
    with bypass_tenant_scope():
        async with session_scope() as db:
            tenant = await db.get(Tenant, tenant_id)
    return bool(tenant and tenant.claude_translate_enabled and tenant.is_usable)


# ── Heartbeat ────────────────────────────────────────────────────────────────


async def record_heartbeat(name: str, detail: str | None = None) -> None:
    async with session_scope() as db:
        row = await db.get(ClaudeWorkerHeartbeat, name)
        now = datetime.now(UTC)
        if row is None:
            db.add(ClaudeWorkerHeartbeat(name=name, seen_at=now, detail=detail))
        else:
            row.seen_at = now
            row.detail = detail


async def last_heartbeat(db: AsyncSession) -> datetime | None:
    """When any worker last asked for work."""
    rows = (await db.execute(select(ClaudeWorkerHeartbeat.seen_at))).scalars().all()
    return max(rows) if rows else None


def worker_online(seen_at: datetime | None, *, now: datetime | None = None) -> bool:
    if seen_at is None:
        return False
    if seen_at.tzinfo is None:
        seen_at = seen_at.replace(tzinfo=UTC)
    limit = timedelta(seconds=get_settings().claude_worker_offline_seconds)
    return (now or datetime.now(UTC)) - seen_at <= limit
