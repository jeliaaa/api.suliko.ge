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

import re
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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


class PasswordChangeRequiredError(AppError):
    """The password was issued by someone else and must be replaced.

    403 rather than 401: the session is valid and the credentials were
    correct. What is refused is the ACTION, until the one-time password an
    invite or an admin reset handed out has been replaced with one only this
    user knows.
    """

    status_code = 403
    error_code = "password_change_required"


class MfaRequiredError(AppError):
    """Login succeeded but the second factor has not been satisfied yet."""

    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "mfa_required"


class PayloadTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    error_code = "payload_too_large"


class UpstreamUnavailableError(AppError):
    """A service we depend on (Google Drive) failed or refused.

    The detail is ours; the upstream's own message is logged, not returned, as
    it can name internal ids.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = "upstream_unavailable"


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


#: PostgreSQL SQLSTATEs that mean "the schema does not have what the code
#: expects", mapped to how to describe the missing thing.
#:
#: Deliberately narrow. These four cannot be provoked by a request — no input
#: this API accepts reaches a table name — so reporting them says nothing about
#: any user's data, only about our own schema. That is the same reasoning that
#: lets the validation handler return Pydantic's errors verbatim.
_SCHEMA_SQLSTATES = {
    "42P01": "table",
    "42703": "column",
    "42883": "function",
    "3F000": "schema",
}

#: `relation "notifications" does not exist` -> notifications
_QUOTED = re.compile(r'"([A-Za-z0-9_.]+)"')


def _missing_schema_object(exc: SQLAlchemyError) -> str | None:
    """Describe the missing table or column, or None if that is not the fault.

    Returns something like ``table "notifications"`` — the identifier only,
    never the driver's full message, which quotes the failing SQL.
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)

    kind = _SCHEMA_SQLSTATES.get(str(sqlstate))
    if kind is None:
        return None

    match = _QUOTED.search(str(orig))
    return f'{kind} "{match.group(1)}"' if match else f"a {kind} it expects"


#: SQLSTATE -> (status, error code, what to tell the caller). PostgreSQL's
#: class 23 ("integrity constraint violation").
_INTEGRITY_SQLSTATES: dict[str, tuple[int, str, str]] = {
    "23505": (
        status.HTTP_409_CONFLICT,
        "conflict",
        "A record with the same details already exists.",
    ),
    "23503": (
        status.HTTP_409_CONFLICT,
        "in_use",
        "This is still used by other records (orders, payments or documents), "
        "so it cannot be removed or changed that way.",
    ),
    "23514": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "A value is outside the range this field allows.",
    ),
    "23502": (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_failed",
        "A required value is missing.",
    ),
}

#: SQLite (the test database) has no SQLSTATE; its messages are stable enough
#: to classify on, and doing so keeps the tests exercising the same mapping.
_SQLITE_INTEGRITY = (
    ("UNIQUE constraint failed", "23505"),
    ("FOREIGN KEY constraint failed", "23503"),
    ("CHECK constraint failed", "23514"),
    ("NOT NULL constraint failed", "23502"),
)


def _describe_integrity_error(exc: IntegrityError) -> tuple[int, str, str]:
    orig = getattr(exc, "orig", None)
    sqlstate = str(getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None) or "")
    if not sqlstate:
        message = str(orig)
        sqlstate = next((code for text, code in _SQLITE_INTEGRITY if text in message), "")
    return _INTEGRITY_SQLSTATES.get(
        sqlstate,
        (
            status.HTTP_409_CONFLICT,
            "conflict",
            "This change conflicts with data already stored.",
        ),
    )


def _validation_summary(errors: list[dict[str, Any]]) -> str:
    """One readable sentence for the top of a form.

    "Request validation failed." told the person looking at the form nothing;
    naming the first field and Pydantic's own message usually tells them
    exactly what to fix. The full list stays in `errors` for field mapping.
    """
    if not errors:
        return "Request validation failed."
    first = errors[0]
    loc = [str(part) for part in first.get("loc", ()) if part not in ("body", "query", "path")]
    field = ".".join(loc)
    message = str(first.get("msg", "is invalid"))
    return f"{field}: {message}" if field else message


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
        # Pydantic's errors describe our own schema, so where and why is safe
        # to return and makes integration far easier to debug. What is NOT
        # returned is `input`: that is the value the caller sent, and for a
        # too-short password or an integration secret, echoing it puts the
        # secret into every log and proxy the response passes through.
        # jsonable_encoder because a custom validator's error carries the
        # raised exception object in `ctx`, which json.dumps cannot encode —
        # without it, any `raise ValueError` in a validator became a 500.
        errors = [
            {key: value for key, value in error.items() if key not in ("input", "url")}
            for error in exc.errors()
        ]
        return _problem(
            request,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "validation_failed",
            _validation_summary(errors),
            {"errors": jsonable_encoder(errors)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _problem(request, exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(IntegrityError)
    async def _integrity(request: Request, exc: IntegrityError) -> JSONResponse:
        # A constraint said no. That is the caller's data meeting a rule, not
        # a fault in the service: a second record with the same unique value,
        # or a delete of something other records still point at. Answered as
        # such rather than as "an internal error occurred", which sends the
        # user to support for something they can fix themselves. The driver's
        # message quotes values, so it is logged, never returned.
        status_code, code, detail = _describe_integrity_error(exc)
        log.info("integrity_error", path=request.url.path, code=code)
        return _problem(request, status_code, code, detail)

    @app.exception_handler(SQLAlchemyError)
    async def _db(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        # A table or column the code expects and the database does not have is
        # a DEPLOYMENT state, not a data error: the service was updated and
        # `alembic upgrade head` was not run. Flattening it to "an internal
        # error occurred" sends whoever is on support hunting for a bug that
        # does not exist, so it gets its own answer.
        missing = _missing_schema_object(exc)
        if missing is not None:
            log.error(
                "schema_out_of_date",
                path=request.url.path,
                missing=missing,
            )
            return _problem(
                request,
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "schema_out_of_date",
                f"The database is missing {missing}, which this build of the API "
                f"requires. Run `alembic upgrade head` on the API server and "
                f"restart the service.",
            )

        # Everything else: database messages quote SQL and sometimes column
        # values. Never echo.
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
