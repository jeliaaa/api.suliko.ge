"""Application settings.

Everything is read from the environment. Nothing secret is ever committed, and
nothing secret is ever stored in the database in the clear — see
``suliko.core.crypto`` for how per-tenant integration credentials are handled.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = "development"
    debug: bool = False

    # ── Database ────────────────────────────────────────────────────────────
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+asyncpg://suliko:suliko@localhost:5432/suliko"),
        description="Async SQLAlchemy URL. Must use the asyncpg driver.",
    )
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_echo: bool = False

    # ── Sessions ────────────────────────────────────────────────────────────
    # Sessions live in Postgres, not Redis: they must be durable, listable
    # ("your active sessions") and instantly revocable on role change or
    # offboarding. See docs/03-SECURITY-AND-TENANCY.md §4.3.
    session_idle_timeout_minutes: int = 30
    session_absolute_timeout_hours: int = 12
    # A step-up action needs 2FA satisfied within this window, regardless of
    # how fresh the session itself is.
    step_up_max_age_minutes: int = 5

    # ── Rate limiting ───────────────────────────────────────────────────────
    # Redis is the right store for this — ephemeral, high-churn counters.
    # Without it the app falls back to an in-process limiter.
    redis_url: str | None = None

    #: Explicit acknowledgement that this deployment runs ONE worker process,
    #: which makes the in-process rate limiter correct and Redis unnecessary.
    #:
    #: This exists because Redis has no supported native Windows build, and a
    #: single-box Windows/IIS deployment is a real target for this product. The
    #: limiter is only wrong when counters must be shared across processes.
    #:
    #: Setting this with more than one worker silently multiplies every rate
    #: limit by the worker count — five login attempts becomes five *per
    #: worker*. If you scale out, install Redis (or Memurai) and unset this.
    rate_limit_single_instance: bool = False

    login_max_attempts_per_account: int = 5
    login_max_attempts_per_ip: int = 20
    login_window_seconds: int = 900  # 15 minutes
    mfa_max_attempts: int = 5
    mfa_window_seconds: int = 300
    write_max_per_minute: int = 120

    # ── Crypto ──────────────────────────────────────────────────────────────
    # Master key wrapping per-tenant data keys (envelope encryption).
    # Generate with:
    #   python -c "import os, base64; print(
    #       base64.urlsafe_b64encode(os.urandom(32)).decode())"
    encryption_master_key: SecretStr = SecretStr("")

    # ── CORS / BFF ──────────────────────────────────────────────────────────
    # The browser never calls this API directly; the Next.js BFF does,
    # server-side. CORS therefore stays narrow.
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # ── App ─────────────────────────────────────────────────────────────────
    app_name: str = "Suliko CRM API"
    api_v1_prefix: str = "/api/v1"

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: PostgresDsn) -> PostgresDsn:
        if "+asyncpg" not in str(v):
            raise ValueError("database_url must use the postgresql+asyncpg driver")
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def validate_for_production(self) -> None:
        """Fail fast at startup rather than run insecurely.

        Every one of these is a configuration mistake that would otherwise be
        discovered by an incident rather than by a deploy.
        """
        problems: list[str] = []

        if not self.encryption_master_key.get_secret_value():
            problems.append("ENCRYPTION_MASTER_KEY is not set")
        if self.debug:
            problems.append("DEBUG must be off in production")
        if self.db_echo:
            problems.append("DB_ECHO must be off in production (it logs query parameters)")
        if not self.redis_url and not self.rate_limit_single_instance:
            problems.append(
                "REDIS_URL is not set. Rate limiting would fall back to a "
                "per-process limiter, which does not hold across processes. "
                "Either set REDIS_URL, or set RATE_LIMIT_SINGLE_INSTANCE=true "
                "to confirm this deployment runs exactly one worker."
            )
        # Loopback is not an insecure transport — it never leaves the machine.
        # A single-box deployment where IIS reverse-proxies to 127.0.0.1
        # legitimately has an http loopback origin.
        insecure = [
            o
            for o in self.cors_origins
            if o.startswith("http://")
            and not o.startswith(("http://localhost", "http://127.0.0.1"))
        ]
        if insecure:
            problems.append(f"CORS origins must be https in production: {insecure}")

        if problems:
            raise RuntimeError("Refusing to start in production:\n  - " + "\n  - ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
