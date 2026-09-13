"""Error types and the RFC 9457 problem-details handler.

Two rules that matter more than the plumbing:

1. **A resource in another tenant returns 404, not 403.** 403 confirms the
   object exists, which is an information leak across a tenant boundary. The
   caller must not be able to distinguish "not yours" from "not there".

2. **Production responses carry no stack traces, SQL, or internal paths.**
   The detail string is chosen by us, never derived from an exception message,
   unless the exception is one of ours.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from suliko.config import get_settings
from suliko.db.tenancy import TenantContextError

log = structlog.get_logger()


class AppError(Exception):
    """Base for errors that are safe to describe to the caller."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "bad_request"

    def __init__(self, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extra = extra


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_code = "conflict"


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "validation_failed"


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "unauthenticated"


class PermissionDeniedError(AppError):
    """The caller is authenticated but lacks the permission.

    Only for permissions within the caller's OWN tenant. Reaching for another
    tenant's data raises NotFoundError instead — see rule 1 above.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_code = "permission_denied"


class StepUpRequiredError(AppError):
    """The action needs a fresh 2FA code.

    A distinct code from plain 403 so the frontend knows to open the step-up
    dialog rather than tell the user they lack access.
    """

    status_code = status.HTTP_403_FORBIDDEN
    error_code = "step_up_required"


class MfaRequiredError(AppError):
    """Login succeeded but the second factor has not been satisfied yet."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "mfa_required"


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_code = "rate_limited"

    def __init__(self, detail: str, retry_after: int, **extra: Any) -> None:
        super().__init__(detail, **extra)
        self.retry_after = retry_after


def _problem(
    request: Request,
    status_code: int,
    error_code: str,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://docs.suliko.ge/errors/{error_code}",
        "title": error_code.replace("_", " ").title(),
        "status": status_code,
        "detail": detail,
        "instance": str(request.url.path),
    }
    if extra:
        body.update(extra)
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type="application/problem+json",
    )


def install_error_handlers(app: FastAPI) -> None:
    settings = get_settings()

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        headers_extra: dict[str, Any] = {}
        if isinstance(exc, RateLimitedError):
            headers_extra["retry_after"] = exc.retry_after

        response = _problem(
            request, exc.status_code, exc.error_code, exc.detail, {**exc.extra, **headers_extra}
        )
        if isinstance(exc, RateLimitedError):
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(TenantContextError)
    async def _tenant_error(request: Request, exc: TenantContextError) -> JSONResponse:
        # Never surfaced verbatim: the message names tenant ids. It is a bug
        # in our code, not something the caller did, so log loudly and return
        # a flat 500.
        log.error("tenant_context_violation", error=str(exc), path=request.url.path)
        return _problem(
            request,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An internal error occurred.",
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Pydantic's errors describe our own schema, not user secrets, so they
        # are safe to return — they make integration far easier to debug.
        return _problem(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            "Request validation failed.",
            {"errors": exc.errors()},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(request, exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(SQLAlchemyError)
    async def _db(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        # Database messages quote SQL and sometimes column values. Never echo.
        log.exception("database_error", path=request.url.path)
        return _problem(
            request,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An internal error occurred.",
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", path=request.url.path)
        detail = (
            f"{type(exc).__name__}: {exc}"
            if not settings.is_production
            else "An internal error occurred."
        )
        return _problem(request, status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error", detail)
