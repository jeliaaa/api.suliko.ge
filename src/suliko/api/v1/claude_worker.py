"""The Claude Code server's side of Claude Translate.

A separate server runs Claude Code. It asks this API for work, one job at a
time, over HTTPS with a bearer token (``CLAUDE_WORKER_TOKENS`` in the server's
``.env``, and optionally ``CLAUDE_WORKER_ALLOWED_IPS``):

    POST /worker/jobs/claim              the oldest waiting job, or 204
    GET  /worker/jobs/{job}/source       the file to translate
    POST /worker/jobs/{job}/result       the Word document (multipart ``file``)
    POST /worker/jobs/{job}/status       ``failed``, or ``queued`` to try later
    POST /worker/heartbeat               "still here" while it has nothing to do

Files stay in Office's own storage: the worker downloads one and uploads its
result, and never holds a storage credential. A job is named by a reference that
carries its organisation (``<tenant id>.<public id>``), because the worker
serves all of them; only a holder of the token can present one, so it is not a
tenant id taken from a browser.

This router sits outside the staff session chain, like the translator portal's
(``api/portal_deps.py``): there is no person and no cookie, and the gateway
secret is not required (``core/gateway.py``), since this server is not the
frontend. The token is what is checked, in constant time. A wrong or missing
token and a refused address are the same flat 401.
"""

from __future__ import annotations

import hmac
import ipaddress
from datetime import UTC, datetime
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, File, Header, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.api.deps import get_bearer_token
from suliko.api.portal_deps import Storage
from suliko.api.v1._files import read_upload
from suliko.api.v1.order_files import storage_failure
from suliko.config import get_settings
from suliko.core.errors import AuthenticationError, ConflictError, NotFoundError
from suliko.db.session import session_scope
from suliko.db.tenancy import tenant_scope
from suliko.domain.claude_translate import (
    claim_next,
    job_in_tenant,
    parse_job_ref,
    record_heartbeat,
    result_name,
    unique_name,
)
from suliko.domain.order_files import (
    attachment_headers,
    list_document_files,
    open_download,
    safe_content_type,
    upload_document_file,
)
from suliko.domain.translation_docx import DOCX_CONTENT_TYPE
from suliko.domain.word_file import ensure_word_document
from suliko.integrations.object_storage import StorageError
from suliko.models.claude_translation import ClaudeJobStatus, ClaudeTranslationJob
from suliko.models.order import Order, OrderDocument
from suliko.models.order_file import OrderFile
from suliko.models.portal import FileKind

log = structlog.get_logger()

#: What this router is mounted under, so the gateway exemption and the route
#: agree. Kept here, not in `core/gateway.py`, only as a pointer: the prefix
#: below is the thing to change together with that file.
router = APIRouter(prefix="/worker", tags=["claude-worker"])

MAX_ERROR = 500


def _tokens() -> list[str]:
    raw = get_settings().claude_worker_tokens.get_secret_value()
    return [token.strip() for token in raw.split(",") if token.strip()]


def _proxy_address(request: Request) -> str | None:
    """The address the reverse proxy in front of this API saw.

    The LAST hop of ``X-Forwarded-For``: the one our proxy appended. The first
    is whatever the caller wrote. With no proxy, the socket's own peer.
    """
    forwarded = request.headers.get("x-forwarded-for")
    candidate = forwarded.split(",")[-1].strip() if forwarded else None
    if not candidate and request.client:
        candidate = request.client.host
    if not candidate:
        return None
    if candidate.startswith("["):
        candidate = candidate[1:].split("]", 1)[0]
    elif candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _address_allowed(address: str | None) -> bool:
    allowed = [a.strip() for a in get_settings().claude_worker_allowed_ips.split(",") if a.strip()]
    if not allowed:
        return True
    if address is None:
        return False
    try:
        ip = ipaddress.ip_address(address)
        return any(ip in ipaddress.ip_network(entry, strict=False) for entry in allowed)
    except ValueError:
        return False


async def require_worker(
    request: Request,
    x_worker_name: Annotated[str | None, Header(max_length=100)] = None,
) -> str:
    """The worker's name, once its token and address check out."""
    tokens = _tokens()
    presented = get_bearer_token(request) or ""
    # Compared against every token, without stopping at the first match.
    matched = [hmac.compare_digest(presented.encode(), token.encode()) for token in tokens]
    if not tokens or not presented or not any(matched):
        log.warning("claude_worker_rejected", reason="token", path=request.url.path)
        raise AuthenticationError("Invalid or missing worker credentials.")
    if not _address_allowed(_proxy_address(request)):
        log.warning("claude_worker_rejected", reason="address", path=request.url.path)
        raise AuthenticationError("Invalid or missing worker credentials.")
    return (x_worker_name or "default").strip()[:100] or "default"


Worker = Annotated[str, Depends(require_worker)]


class ClaimedOut(BaseModel):
    job_id: str
    #: The order's number in its organisation, for the worker's own logs.
    order_number: int
    file_name: str
    content_type: str
    size_bytes: int
    #: Office language codes, and their English names for the prompt.
    source_language: str
    target_language: str
    source_language_name: str
    target_language_name: str
    #: The order's translation instructions; null when there are none.
    instructions: str | None
    attempts: int
    #: What the result is to be called.
    output_file_name: str
    #: Where to fetch the file, relative to the API root.
    source_path: str


@router.post("/jobs/claim", response_model=ClaimedOut, responses={204: {"description": "Empty"}})
async def claim(worker: Worker) -> ClaimedOut | Response:
    """The oldest waiting job, now yours. 204 when nothing waits.

    Atomic: two workers asking at once never get the same job.
    """
    await record_heartbeat(worker)
    job = await claim_next()
    if job is None:
        return Response(status_code=204)
    prefix = get_settings().api_v1_prefix
    return ClaimedOut(
        job_id=job.ref,
        order_number=job.order_number,
        file_name=job.file_name,
        content_type=job.content_type,
        size_bytes=job.size_bytes,
        source_language=job.source_language,
        target_language=job.target_language,
        source_language_name=job.source_language_name,
        target_language_name=job.target_language_name,
        instructions=job.instructions,
        attempts=job.attempts,
        output_file_name=job.output_name,
        source_path=f"{prefix}/worker/jobs/{job.ref}/source",
    )


async def _translating(db: AsyncSession, ref: str) -> ClaudeTranslationJob:
    job = await job_in_tenant(db, ref)
    if job.status is not ClaudeJobStatus.TRANSLATING:
        raise ConflictError(
            f"This job is {job.status.value}, not translating.", status=job.status.value
        )
    return job


@router.get("/jobs/{ref}/source")
async def download_source(ref: str, storage: Storage, _: Worker) -> StreamingResponse:
    """The file to translate. Only while the job is the worker's."""
    tenant_id, _public = parse_job_ref(ref)
    with tenant_scope(tenant_id):
        async with session_scope() as db:
            job = await _translating(db, ref)
            source = await db.get(OrderFile, job.source_file_id) if job.source_file_id else None
            if source is None or source.deleted_at is not None:
                raise NotFoundError("That file is no longer there.")
    try:
        body = await open_download(storage, source)
    except StorageError as exc:
        raise storage_failure(exc) from exc
    return StreamingResponse(
        body,
        media_type=safe_content_type(source.content_type),
        headers={**attachment_headers(source.file_name), "Content-Length": str(source.size_bytes)},
    )


class ResultOut(BaseModel):
    status: Literal["translated"] = "translated"
    output_file_id: str
    output_file_name: str


@router.post("/jobs/{ref}/result", response_model=ResultOut)
async def post_result(
    ref: str,
    file: Annotated[UploadFile, File()],
    storage: Storage,
    _: Worker,
) -> ResultOut:
    """File the Word document under the document the source belongs to, and
    mark the job translated: one step, so there is no job marked done with no
    file behind it.

    Named as the job says (``<name>_Translated.docx``), with a number if the
    document already has a file by that name. Nothing is replaced.
    """
    tenant_id, _public = parse_job_ref(ref)
    content = await read_upload(file, get_settings().order_file_max_bytes)
    ensure_word_document(content)

    problem: str | None = None
    with tenant_scope(tenant_id):
        async with session_scope() as db:
            job = await _translating(db, ref)
            document = (
                await db.get(OrderDocument, job.order_document_id)
                if job.order_document_id
                else None
            )
            order = await db.get(Order, job.order_id)
            if document is None or order is None:
                job.status = ClaudeJobStatus.FAILED
                job.error = "The document was removed before it could be translated."
                job.finished_at = datetime.now(UTC)
                problem = job.error
            else:
                # Moved first, so a second upload of the same job finds it
                # already translated; the upload that follows is in the same
                # transaction, so a storage failure puts the job back as it was.
                won = (
                    await db.execute(
                        update(ClaudeTranslationJob)
                        .where(
                            ClaudeTranslationJob.id == job.id,
                            ClaudeTranslationJob.tenant_id == tenant_id,
                            ClaudeTranslationJob.status == ClaudeJobStatus.TRANSLATING,
                        )
                        .values(
                            status=ClaudeJobStatus.TRANSLATED,
                            error=None,
                            finished_at=datetime.now(UTC),
                        )
                        .returning(ClaudeTranslationJob.id)
                    )
                ).first()
                if won is None:
                    raise ConflictError("This job is no longer translating.")
                taken = {f.file_name for f in await list_document_files(db, document.id)}
                name = unique_name(result_name(job.file_name), taken)
                try:
                    stored = await upload_document_file(
                        db,
                        storage,
                        order=order,
                        document=document,
                        kind=FileKind.TRANSLATION,
                        file_name=name,
                        content=content,
                        content_type=DOCX_CONTENT_TYPE,
                        uploaded_by=f"claude:{job.requested_by_user_id or 0}",
                    )
                except StorageError as exc:
                    raise storage_failure(exc) from exc
                await db.execute(
                    update(ClaudeTranslationJob)
                    .where(ClaudeTranslationJob.id == job.id)
                    .values(result_file_id=stored.id)
                )
                log.info("claude_job_translated", job=ref, size=len(content))
                return ResultOut(output_file_id=stored.public_id, output_file_name=name)
    raise ConflictError(problem or "The document was removed.")


class StatusIn(BaseModel):
    #: ``failed``: give up (the person sees the message and may retry).
    #: ``queued``: try again later, such as a usage limit; the attempt it used
    #: is given back.
    status: Literal["failed", "queued"]
    error_message: str | None = Field(default=None, max_length=MAX_ERROR)


class StatusOut(BaseModel):
    status: ClaudeJobStatus


@router.post("/jobs/{ref}/status", response_model=StatusOut)
async def report_status(ref: str, payload: StatusIn, _: Worker) -> StatusOut:
    """Report a job that did not come to a result. A success is reported by
    sending the result."""
    tenant_id, public_id = parse_job_ref(ref)
    message = (payload.error_message or "").strip()[:MAX_ERROR] or None
    new = ClaudeJobStatus(payload.status)
    values: dict[str, object] = {"status": new, "error": message}
    if new is ClaudeJobStatus.FAILED:
        values["finished_at"] = datetime.now(UTC)
    else:
        values["attempts"] = case(
            (ClaudeTranslationJob.attempts > 0, ClaudeTranslationJob.attempts - 1), else_=0
        )
    with tenant_scope(tenant_id):
        async with session_scope() as db:
            await job_in_tenant(db, ref)
            won = (
                await db.execute(
                    update(ClaudeTranslationJob)
                    .where(
                        ClaudeTranslationJob.tenant_id == tenant_id,
                        ClaudeTranslationJob.public_id == public_id,
                        ClaudeTranslationJob.status == ClaudeJobStatus.TRANSLATING,
                    )
                    .values(**values)
                    .returning(ClaudeTranslationJob.id)
                )
            ).first()
            if won is None:
                current = (
                    await db.execute(
                        select(ClaudeTranslationJob.status).where(
                            ClaudeTranslationJob.tenant_id == tenant_id,
                            ClaudeTranslationJob.public_id == public_id,
                        )
                    )
                ).scalar_one()
                raise ConflictError(
                    f"This job is {current.value}, not translating.", status=current.value
                )
    log.info("claude_job_reported", job=ref, status=new.value)
    return StatusOut(status=new)


class HeartbeatIn(BaseModel):
    #: Free text, such as the worker's version. Kept for support.
    detail: str | None = Field(default=None, max_length=MAX_ERROR)


@router.post("/heartbeat", status_code=204)
async def heartbeat(worker: Worker, payload: HeartbeatIn | None = None) -> Response:
    await record_heartbeat(worker, payload.detail if payload else None)
    return Response(status_code=204)
