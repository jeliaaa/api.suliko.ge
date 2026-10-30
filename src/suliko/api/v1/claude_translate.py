"""Claude Translate from an order: staff pick files, a Claude Code server translates.

The person opens the order, ticks the source files to translate and confirms.
Each ticked file becomes a queued job; nothing else is touched, and nothing is
translated when a file is merely uploaded. The worker (``api/v1/claude_worker.py``)
does the rest and files a Word document under the same document's Translation
files, where it opens in the side-by-side editor like any other. It is a draft
for the translator, never a finished document, and the order's own status is
not changed.

Only organisations a superuser switched on (``tenants.claude_translate_enabled``,
on the platform tenant page) have these routes; the others are answered 403
``claude_translate_not_enabled``. Nothing is charged to a page balance.

Every file id in a request is looked up among the live Source files of THIS
order's documents, so a file of another order or organisation is a 404 and is
never queued.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import CurrentSession, Db, require
from suliko.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from suliko.domain.claude_translate import last_heartbeat, requeue_stuck, worker_online
from suliko.domain.order_files import FILE_ID_PATTERN, downloadable, new_public_id
from suliko.models.claude_translation import (
    ACTIVE_STATUSES,
    ClaudeJobStatus,
    ClaudeTranslationJob,
)
from suliko.models.order import Order, OrderDocument
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind
from suliko.models.tenant import Tenant
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

log = structlog.get_logger()

router = APIRouter(prefix="/orders", tags=["claude-translate"])

#: Files in one request. An order with more source files than this is split.
MAX_FILES = 50


class ClaudeTranslateNotEnabledError(PermissionDeniedError):
    error_code = "claude_translate_not_enabled"


class JobActiveError(ConflictError):
    error_code = "claude_job_active"


class RetranslateNeedsConfirmationError(ConflictError):
    error_code = "claude_retranslate_needs_confirmation"


async def feature_enabled(db: Db, session: CurrentSession) -> None:
    """403 unless the caller's organisation has been given Claude Translate."""
    tenant = await db.get(Tenant, session.tenant_id)
    if tenant is None or not tenant.claude_translate_enabled:
        raise ClaudeTranslateNotEnabledError("Claude Translate is not enabled for this account.")


Enabled = Annotated[None, Depends(feature_enabled)]


class JobOut(BaseModel):
    id: str
    #: The source file, null once it has been removed.
    file_id: str | None
    file_name: str
    document_id: int | None
    status: ClaudeJobStatus
    attempts: int
    error: str | None
    source_language: str
    target_language: str
    #: The Word document, once translated and while it is still there.
    result_file_id: str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class JobListOut(BaseModel):
    jobs: list[JobOut]
    #: Whether a worker has asked for work lately. Jobs wait either way.
    worker_online: bool
    worker_last_seen: datetime | None


class PickableFileOut(BaseModel):
    id: str
    name: str
    content_type: str
    size_bytes: int
    document_id: int
    source_language: str
    target_language: str
    #: False for a file only the Order Vault can open: it cannot be sent.
    downloadable: bool
    #: Its latest job, if it ever had one.
    job_status: ClaudeJobStatus | None
    #: A finished translation of this file is still on the document.
    has_output: bool


class PickableOut(BaseModel):
    files: list[PickableFileOut]
    #: What the worker will be told about the order; empty when none is set.
    instructions: str | None


class CreateJobsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[str] = Field(min_length=1, max_length=MAX_FILES)
    #: Said yes to translating files that already have a Claude translation.
    #: The old output is never replaced; the new one gets a number.
    confirm_retranslate: bool = False

    @field_validator("files")
    @classmethod
    def _ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("A file can be picked once.")
        if not all(FILE_ID_PATTERN.fullmatch(v) for v in value):
            raise ValueError("Not a file id.")
        return value


async def _order(db: AsyncSession, order_id: int) -> Order:
    order = await db.get(Order, order_id)
    if order is None:
        raise NotFoundError("Order not found.")
    return order


async def _public_ids(db: AsyncSession, jobs: list[ClaudeTranslationJob]) -> dict[int, str]:
    ids = {i for job in jobs for i in (job.source_file_id, job.result_file_id) if i is not None}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(OrderFile.id, OrderFile.public_id).where(
                OrderFile.id.in_(ids), OrderFile.deleted_at.is_(None)
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


def _job_out(job: ClaudeTranslationJob, files: dict[int, str]) -> JobOut:
    return JobOut(
        id=job.public_id,
        file_id=files.get(job.source_file_id) if job.source_file_id else None,
        file_name=job.file_name,
        document_id=job.order_document_id,
        status=job.status,
        attempts=job.attempts,
        error=job.error,
        source_language=job.source_language,
        target_language=job.target_language,
        result_file_id=files.get(job.result_file_id) if job.result_file_id else None,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


async def _jobs_of(db: AsyncSession, order_id: int) -> list[ClaudeTranslationJob]:
    return list(
        (
            await db.execute(
                select(ClaudeTranslationJob)
                .where(ClaudeTranslationJob.order_id == order_id)
                .order_by(ClaudeTranslationJob.id.desc())
            )
        )
        .scalars()
        .all()
    )


@router.get("/{order_id}/claude-translate/files", response_model=PickableOut)
async def list_pickable_files(
    order_id: int,
    db: Db,
    _enabled: Enabled,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> PickableOut:
    """The order's Source files, with what is already known about each: what the
    dialog lists."""
    order = await _order(db, order_id)
    documents = {
        d.id: d
        for d in (await db.execute(select(OrderDocument).where(OrderDocument.order_id == order_id)))
        .scalars()
        .all()
    }
    if not documents:
        return PickableOut(files=[], instructions=order.translation_instructions)
    files = list(
        (
            await db.execute(
                select(OrderFile)
                .where(
                    OrderFile.order_document_id.in_(documents),
                    OrderFile.deleted_at.is_(None),
                    OrderFile.kind == FileKind.SOURCE,
                )
                .order_by(OrderFile.id)
            )
        )
        .scalars()
        .all()
    )
    jobs = await _jobs_of(db, order_id)
    latest: dict[int, ClaudeTranslationJob] = {}
    for job in jobs:  # newest first
        if job.source_file_id is not None:
            latest.setdefault(job.source_file_id, job)
    outputs = await _live_outputs(db, jobs)
    return PickableOut(
        files=[
            PickableFileOut(
                id=f.public_id,
                name=f.file_name,
                content_type=f.content_type,
                size_bytes=f.size_bytes,
                document_id=documents[f.order_document_id].id,
                source_language=documents[f.order_document_id].source_language,
                target_language=documents[f.order_document_id].target_language,
                downloadable=downloadable(f),
                job_status=latest[f.id].status if f.id in latest else None,
                has_output=f.id in outputs,
            )
            for f in files
            if f.order_document_id in documents
        ],
        instructions=order.translation_instructions,
    )


async def _live_outputs(db: AsyncSession, jobs: list[ClaudeTranslationJob]) -> set[int]:
    """Source file ids that have a finished translation still on the document."""
    translated = [j for j in jobs if j.status is ClaudeJobStatus.TRANSLATED and j.result_file_id]
    if not translated:
        return set()
    alive = set(
        (
            await db.execute(
                select(OrderFile.id).where(
                    OrderFile.id.in_([j.result_file_id for j in translated]),
                    OrderFile.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return {j.source_file_id for j in translated if j.result_file_id in alive and j.source_file_id}


@router.post(
    "/{order_id}/claude-translate/jobs",
    response_model=list[JobOut],
    status_code=http_status.HTTP_201_CREATED,
)
async def create_jobs(
    order_id: int,
    payload: CreateJobsIn,
    db: Db,
    session: CurrentSession,
    _enabled: Enabled,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> list[JobOut]:
    """Queue one job per ticked file. All or nothing."""
    order = await _order(db, order_id)
    documents = {
        d.id: d
        for d in (await db.execute(select(OrderDocument).where(OrderDocument.order_id == order_id)))
        .scalars()
        .all()
    }
    rows = (
        (
            await db.execute(
                select(OrderFile).where(
                    OrderFile.public_id.in_(payload.files),
                    OrderFile.order_document_id.in_(documents or [-1]),
                    OrderFile.deleted_at.is_(None),
                    OrderFile.kind == FileKind.SOURCE,
                )
            )
        )
        .scalars()
        .all()
    )
    # A manipulated request naming another order's file, or a translation, or a
    # removed one: refused as not found, whatever else was in it.
    if len(rows) != len(payload.files):
        raise NotFoundError("File not found.")
    by_public = {row.public_id: row for row in rows}
    chosen = [by_public[i] for i in payload.files]

    not_sendable = [f.file_name for f in chosen if not downloadable(f)]
    if not_sendable:
        raise ValidationError(
            "These files are kept in the Order Vault and cannot be translated from here: "
            + ", ".join(not_sendable)
            + ".",
            files=not_sendable,
        )

    existing = await _jobs_of(db, order_id)
    chosen_ids = {f.id for f in chosen}
    busy = sorted(
        {
            j.file_name
            for j in existing
            if j.source_file_id in chosen_ids and j.status in ACTIVE_STATUSES
        }
    )
    if busy:
        raise JobActiveError(
            "These files are already waiting for Claude or being translated: "
            + ", ".join(busy)
            + ".",
            files=busy,
        )
    outputs = await _live_outputs(db, existing)
    again = sorted({f.file_name for f in chosen if f.id in outputs})
    if again and not payload.confirm_retranslate:
        raise RetranslateNeedsConfirmationError(
            "These files already have a Claude translation: "
            + ", ".join(again)
            + ". Confirm to translate them again.",
            files=again,
        )

    jobs = [
        ClaudeTranslationJob(
            public_id=new_public_id(),
            order_id=order.id,
            order_document_id=f.order_document_id,
            source_file_id=f.id,
            status=ClaudeJobStatus.QUEUED,
            file_name=f.file_name,
            content_type=f.content_type,
            source_language=(document := documents[f.order_document_id or 0]).source_language,
            target_language=document.target_language,
            instructions=order.translation_instructions,
            attempts=0,
            requested_by_user_id=session.user_id,
        )
        for f in chosen
    ]
    db.add_all(jobs)
    try:
        await db.flush()
    except IntegrityError as exc:
        # Two clicks at once: the database's one-active-job-per-file rule won.
        raise JobActiveError("One of these files is already waiting for Claude.") from exc

    from suliko.core.audit import record

    await record(
        db,
        session,
        action="order.claude_translation_requested",
        entity_type="order",
        entity_id=order_id,
        after={"files": [f.file_name for f in chosen]},
    )
    log.info("claude_jobs_queued", order=order_id, count=len(jobs))
    files = await _public_ids(db, jobs)
    return [_job_out(job, files) for job in jobs]


@router.get("/{order_id}/claude-translate/jobs", response_model=JobListOut)
async def list_jobs(
    order_id: int,
    db: Db,
    _enabled: Enabled,
    _: Annotated[object, Depends(require(Permission.ORDERS_READ))],
) -> JobListOut:
    """The order's jobs, newest first, and whether a worker is about.

    Looking is also when a job the worker has held too long is put back, so the
    panel never shows "translating" for a job nobody is working on.
    """
    await _order(db, order_id)
    await requeue_stuck(db)
    jobs = await _jobs_of(db, order_id)
    files = await _public_ids(db, jobs)
    seen = await last_heartbeat(db)
    return JobListOut(
        jobs=[_job_out(job, files) for job in jobs],
        worker_online=worker_online(seen),
        worker_last_seen=seen,
    )


async def _job(db: AsyncSession, order_id: int, job_id: str) -> ClaudeTranslationJob:
    job = (
        await db.execute(
            select(ClaudeTranslationJob).where(
                ClaudeTranslationJob.public_id == job_id,
                ClaudeTranslationJob.order_id == order_id,
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise NotFoundError("Job not found.")
    return job


@router.post("/{order_id}/claude-translate/jobs/{job_id}/retry", response_model=JobOut)
async def retry_job(
    order_id: int,
    job_id: str,
    db: Db,
    session: CurrentSession,
    _enabled: Enabled,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> JobOut:
    """A failed job goes back in the queue with its attempts counted afresh."""
    job = await _job(db, order_id, job_id)
    source = await db.get(OrderFile, job.source_file_id) if job.source_file_id else None
    if source is None or source.deleted_at is not None:
        raise ConflictError("The file was removed, so there is nothing to translate again.")
    won = (
        await db.execute(
            update(ClaudeTranslationJob)
            .where(
                ClaudeTranslationJob.id == job.id,
                ClaudeTranslationJob.status == ClaudeJobStatus.FAILED,
            )
            .values(
                status=ClaudeJobStatus.QUEUED,
                attempts=0,
                error=None,
                started_at=None,
                finished_at=None,
            )
            .returning(ClaudeTranslationJob.id)
        )
    ).first()
    if won is None:
        raise ConflictError("Only a failed job can be retried.", status=job.status.value)
    await db.refresh(job)
    await _audit(db, session, order_id, "order.claude_translation_retried", job)
    return _job_out(job, await _public_ids(db, [job]))


@router.post("/{order_id}/claude-translate/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    order_id: int,
    job_id: str,
    db: Db,
    session: CurrentSession,
    _enabled: Enabled,
    _: Annotated[object, Depends(require(Permission.ORDERS_WRITE))],
) -> JobOut:
    """Take a waiting job out of the queue. Not once the worker has it."""
    job = await _job(db, order_id, job_id)
    won = (
        await db.execute(
            update(ClaudeTranslationJob)
            .where(
                ClaudeTranslationJob.id == job.id,
                ClaudeTranslationJob.status == ClaudeJobStatus.QUEUED,
            )
            .values(status=ClaudeJobStatus.CANCELLED, finished_at=datetime.now(UTC))
            .returning(ClaudeTranslationJob.id)
        )
    ).first()
    if won is None:
        raise ConflictError("Only a waiting job can be cancelled.", status=job.status.value)
    await db.refresh(job)
    await _audit(db, session, order_id, "order.claude_translation_cancelled", job)
    return _job_out(job, await _public_ids(db, [job]))


async def _audit(
    db: AsyncSession,
    session: AuthenticatedSession,
    order_id: int,
    action: str,
    job: ClaudeTranslationJob,
) -> None:
    from suliko.core.audit import record

    await record(
        db,
        session,
        action=action,
        entity_type="order",
        entity_id=order_id,
        after={"file_name": job.file_name},
    )
