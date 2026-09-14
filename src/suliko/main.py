"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from suliko import __version__
from suliko.api.v1.router import api_router
from suliko.config import get_settings
from suliko.core.errors import install_error_handlers
from suliko.core.gateway import GatewayMiddleware
from suliko.db.session import dispose_engine
from suliko.db.tenancy import install_tenant_filter


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

    structlog.get_logger().info("startup", version=__version__, environment=settings.environment)
    yield
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

    return app


app = create_app()
