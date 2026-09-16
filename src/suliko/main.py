"""FastAPI application factory."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from suliko import __version__
from suliko.api.v1.router import api_router
from suliko.config import get_settings
from suliko.core.errors import install_error_handlers
from suliko.core.gateway import GatewayMiddleware
from suliko.db.session import dispose_engine
from suliko.db.tenancy import install_tenant_filter
from suliko.integrations.google_drive import close_drive_client


def migration_head() -> str:
    """The revision this build of the code expects the database to be at.

    Read from the migration files rather than hardcoded, so it cannot go stale
    the next time someone adds a revision: the head is the one revision that
    nothing else names as its ``down_revision``.
    """
    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"

    revisions: set[str] = set()
    parents: set[str] = set()
    for path in versions.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern, bucket in (
            (r'^revision:?\s*(?::\s*str\s*)?=\s*"([^"]+)"', revisions),
            (r'^down_revision:?\s*(?::[^=]+)?=\s*"([^"]+)"', parents),
        ):
            match = re.search(pattern, text, re.MULTILINE)
            if match:
                bucket.add(match.group(1))

    heads = revisions - parents
    # A merge point or an empty directory: say nothing rather than guess.
    return next(iter(heads)) if len(heads) == 1 else ""


def configure_logging(debug: bool) -> None:
    """Structured JSON logs in production, human-readable in development."""
    logging.basicConfig(format="%(message)s", level=logging.DEBUG if debug else logging.INFO)

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.dev.ConsoleRenderer() if debug else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if debug else logging.INFO
        ),
        cache_logger_on_first_use=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    if settings.is_production:
        # Fail the deploy rather than serve insecurely.
        settings.validate_for_production()

    # Must run before any query. Registering it here rather than at import
    # time keeps the ORM events out of the way of Alembic and of tests that
    # deliberately query across tenants.
    install_tenant_filter()

    if not settings.mfa_enforced:
        # Warning rather than refusal: this is a deliberate, configured
        # choice. But it must be impossible to miss in the logs, and it must
        # show up on every single boot until it is turned back on.
        structlog.get_logger().warning(
            "mfa_disabled",
            detail=(
                "MFA_ENFORCED is false. Every account, including superusers, "
                "signs in with a password alone. Set MFA_ENFORCED=true once "
                "the enrolment screen exists."
            ),
        )
    elif not settings.mfa_require_enrolment:
        # The weaker of the two states, and the easier one to forget about:
        # 2FA looks switched on, and for anyone who has enrolled it is. What
        # is off is the requirement to enrol at all.
        structlog.get_logger().warning(
            "mfa_enrolment_not_required",
            detail=(
                "MFA_REQUIRE_ENROLMENT is false. A factor is still demanded "
                "from anyone who has one enrolled, but owners, admins and "
                "superusers with no factor sign in on their password alone. "
                "Set MFA_REQUIRE_ENROLMENT=true once the enrolment screen "
                "exists — until then it locks those accounts out instead."
            ),
        )

    structlog.get_logger().info("startup", version=__version__, environment=settings.environment)
    yield
    await close_drive_client()
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.debug)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        # The OpenAPI document is the frontend's source of generated types.
        # Kept on in production too: it is behind the same auth as everything
        # else and describes no secrets.
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    # Order matters: middleware added later runs EARLIER. The gateway is added
    # after CORS so it runs first and rejects unknown callers before anything
    # else touches the request — but it exempts OPTIONS so CORS preflight,
    # which browsers send without custom headers, still succeeds.
    app.add_middleware(GatewayMiddleware)

    # Narrow by design: only the BFF calls this API, server-side.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Suliko-Gateway"],
        max_age=600,
    )

    install_error_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/health", tags=["meta"], include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/health/ready", tags=["meta"], include_in_schema=False)
    async def ready() -> JSONResponse:
        """Is this API actually able to serve requests?

        `/health` answers "is the process up", which stays green while the
        database is unreachable or a migration behind — and a schema one
        migration behind is the single most common way this deployment breaks,
        because the frontend ships on push and the API is a manual pull.

        This answers the question that matters instead: can we reach the
        database, and is its schema the one this build expects. One request,
        and the answer names the fix.
        """
        from sqlalchemy import text

        from suliko.db.session import get_sessionmaker

        expected = migration_head()

        try:
            async with get_sessionmaker()() as db:
                applied = await db.scalar(text("SELECT version_num FROM alembic_version"))
        # Deliberately broad: whatever went wrong, the answer is the same
        # shape and the type name is the useful part of it.
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unavailable",
                    "version": __version__,
                    "database": "unreachable",
                    "detail": type(exc).__name__,
                },
            )

        if applied != expected:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "migration_pending",
                    "version": __version__,
                    "database": "reachable",
                    "schema_applied": applied,
                    "schema_expected": expected,
                    "detail": (
                        "The database schema is not the one this build expects. "
                        "Run `alembic upgrade head` and restart the service."
                    ),
                },
            )

        return JSONResponse(
            content={
                "status": "ok",
                "version": __version__,
                "database": "reachable",
                "schema_applied": applied,
            }
        )

    return app


app = create_app()
