"""Application settings.

Everything is read from the environment. Nothing secret is ever committed, and
nothing secret is ever stored in the database in the clear — see
``suliko.core.crypto`` for how per-tenant integration credentials are handled.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, PostgresDsn, SecretStr, field_validator
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

    #: How long a password-reset link stays usable. Long enough to survive a
    #: slow mail relay and someone reading their inbox after lunch; short
    #: enough that a link sitting in an archived mailbox is not a standing key.
    password_reset_ttl_minutes: int = 60

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

    #: Reset requests per account and per IP per window. Lower than the login
    #: limits: a reset request sends mail to a third party, so an unthrottled
    #: endpoint is both an enumeration oracle and a way to use us to spam
    #: someone else's inbox.
    password_reset_max_per_account: int = 3
    password_reset_max_per_ip: int = 10
    password_reset_window_seconds: int = 3600  # 1 hour

    #: Sign-ups per IP per window. `POST /auth/signup` is the only
    #: unauthenticated endpoint that creates a TENANT, so an unthrottled one
    #: lets a single address fill the tenants table overnight.
    signup_max_per_ip: int = 3
    signup_window_seconds: int = 3600  # 1 hour

    #: Invitations (staff and translator) per TENANT per window. Each one
    #: sends mail from the platform's own address with text the tenant wrote,
    #: so an unthrottled invite form is a way to use us to spam or phish.
    invite_max_per_tenant: int = 30
    invite_window_seconds: int = 86400  # 1 day
    #: How long an invitation's set-your-password link stays usable.
    invite_link_ttl_hours: int = 168  # 7 days

    #: How long a signup's email-confirmation link stays usable. Longer than
    #: a password reset: nothing is blocked on it (see `api/v1/auth.py`), so
    #: there is no urgency pushing the other way, and an owner who signs up
    #: on a Friday should not come back Monday to a dead link.
    email_verification_ttl_hours: int = 72
    #: Resends per ACCOUNT per window. Authenticated — the caller already
    #: holds a session for the account — so this bounds "click resend
    #: repeatedly", not enumeration or third-party spam the way the anonymous
    #: forgot-password limiter does.
    email_verification_resend_max: int = 5
    email_verification_resend_window_seconds: int = 3600  # 1 hour

    # ── Crypto ──────────────────────────────────────────────────────────────
    # Master key wrapping per-tenant data keys (envelope encryption).
    # Generate with:
    #   python -c "import os, base64; print(
    #       base64.urlsafe_b64encode(os.urandom(32)).decode())"
    encryption_master_key: SecretStr = SecretStr("")

    # ── Two-factor authentication ───────────────────────────────────────────
    #: Master switch for the second factor.
    #:
    #: When false, login completes on the password alone: no challenge is
    #: issued, no enrolment is demanded, and an already-enrolled factor is
    #: ignored. Enrolled secrets are NOT deleted, so flipping this back to
    #: true restores the previous behaviour with no re-enrolment.
    #:
    #: Intended as a temporary measure while the enrolment UI is being built.
    #: Startup logs a warning on every boot while it is off, deliberately —
    #: this is not a setting that should quietly become permanent, in a system
    #: holding client identity documents and bank details.
    mfa_enforced: bool = True

    #: Whether a role in MFA_REQUIRED_ROLES may sign in with NO factor enrolled.
    #:
    #: False (the default) lets them in on the password alone. True fails the
    #: login closed instead, which is the stronger policy and the eventual
    #: intent — but it can only be honest once a user can enrol a factor for
    #: themselves. There is no enrolment screen yet: enrolling means running
    #: `suliko enrol-mfa` on the server, so failing closed does not prompt
    #: anyone to add a factor, it simply locks out the owner and every admin
    #: of every tenant with no way for them to act on it.
    #:
    #: This is deliberately SEPARATE from MFA_ENFORCED. With the default pair
    #: (enforced, enrolment not required) a user who HAS a factor is still
    #: challenged for it — so 2FA keeps working for everyone who has enrolled,
    #: and only the dead end is removed. Turn this on the day enrolment ships.
    mfa_require_enrolment: bool = False

    # ── BFF gateway ─────────────────────────────────────────────────────────
    #: Shared secret the Vercel frontend presents in X-Suliko-Gateway.
    #:
    #: Required when the API is internet-facing (the Vercel topology), because
    #: Vercel has no stable egress IPs to allow-list. Optional and inert when
    #: the API is loopback-only. Defence in depth — every endpoint still
    #: enforces sessions, permissions and tenancy behind it.
    bff_shared_secret: SecretStr = SecretStr("")

    #: Set when this API is reachable from the internet rather than only from
    #: localhost. Turns the missing-gateway-secret check below into an error.
    public_api: bool = False

    # ── Translator portal (suliko.ge) ───────────────────────────────────────
    #: HMAC key shared with the suliko.ge Next.js server. It signs the identity
    #: assertion that server sends with every portal call, and the file tickets
    #: that let a browser move one file directly. See security/portal_tokens.py.
    #: Empty disables the portal: every portal call is refused with 401.
    #:
    #: Separate from BFF_SHARED_SECRET on purpose. That one proves "the caller
    #: is our frontend"; this one vouches for WHICH suliko.ge user is acting,
    #: and is never itself sent over the wire.
    portal_shared_secret: SecretStr = SecretStr("")
    portal_assertion_max_age_seconds: int = 60
    portal_ticket_max_age_seconds: int = 300

    #: Personal-order files live in the database, so they are capped harder
    #: than order files, which go to object storage.
    personal_file_max_bytes: int = 25 * 1024 * 1024
    #: Read from DRIVE_FILE_MAX_BYTES too, so a server .env written for the
    #: Google Drive era keeps its limit.
    order_file_max_bytes: int = Field(
        default=50 * 1024 * 1024,
        validation_alias=AliasChoices("ORDER_FILE_MAX_BYTES", "DRIVE_FILE_MAX_BYTES"),
    )

    # ── Order file storage ──────────────────────────────────────────────────
    # Suliko stores every bureau's order files itself; see
    # integrations/object_storage.py. Unset: order data still works, file
    # routes report that storage is not configured.
    storage_backend: Literal["s3", "local"] | None = None
    #: STORAGE_BACKEND=local: the directory files are written under.
    storage_local_dir: str | None = None
    #: STORAGE_BACKEND=s3. Leave the endpoint unset for AWS itself — it
    #: follows from the region. Cloudflare R2 uses region "auto".
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None
    #: "path" (bucket in the path) works with every provider; "virtual"
    #: (bucket in the host name) is AWS's preferred form.
    s3_addressing_style: Literal["path", "virtual"] = "path"
    #: How long a removed file stays restorable before `suliko purge-files`
    #: deletes its bytes.
    file_retention_days: int = 30

    # ── Outbound email (platform) ───────────────────────────────────────────
    # AUTH mail only: password resets, and later invites and welcome mail.
    #
    # Deliberately platform-level rather than the per-tenant SMTP integration.
    # A password reset is requested BEFORE we know which tenant the address
    # belongs to, and a bureau whose SMTP credentials have lapsed must never
    # be the reason one of its people cannot get back into their account.
    # Business mail — client confirmations, document delivery — keeps using
    # the tenant's own SMTP, because it has to come from the bureau's address.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr = SecretStr("")
    #: STARTTLS on a submission port (587). The usual choice.
    smtp_starttls: bool = True
    #: Implicit TLS from the first byte (465). Mutually exclusive with the above.
    smtp_ssl: bool = False
    smtp_from_email: str | None = None
    smtp_from_name: str = "Suliko"
    #: Sending happens in a worker thread; this bounds how long it can block one.
    smtp_timeout_seconds: int = 20

    #: Public base URL of the frontend. Password-reset links are built from it.
    #:
    #: NEVER derived from a request header. `Host` is attacker-controlled, and
    #: a reset link pointing at an attacker's domain is account takeover — the
    #: classic host-header poisoning bug, and the reason this is configuration.
    app_url: str = "http://localhost:3000"

    #: suliko.ge's own base URL — a DIFFERENT site from `app_url` (this
    #: bureau's Suliko Office). An invite that matches no suliko.ge account links here so
    #: the invitee can register; see `PortalAccountInvite` and
    #: `domain/portal.py`'s registration-link helpers.
    suliko_site_url: str = "https://suliko.ge"

    # ── suliko.ge's backend (sign-in and the user directory) ───────────────
    #: Base URL of suliko.ge's .NET backend, e.g. https://content.api24.ge.
    #: Set, people sign in to Suliko Office with their suliko.ge email or phone
    #: and password, which suliko.ge checks (`integrations/suliko_backend.py`).
    #: Unset: that is off, and sign-in works on Office's own passwords alone.
    suliko_api_url: str | None = None
    #: The key suliko.ge's `Office:ApiKey` is set to; sent as `X-Office-Key` on
    #: its user-directory endpoints. A password check needs none.
    suliko_api_key: SecretStr = SecretStr("")
    suliko_api_timeout_seconds: float = 10.0
    #: Where a person resets a suliko.ge password — sent to those who ask to
    #: reset it here, since Office does not hold it.
    suliko_password_reset_url: str = "https://suliko.ge/login"  # noqa: S105 -- a URL

    # ── CORS ────────────────────────────────────────────────────────────────
    # The Next.js BFF calls this API server-side. The one exception is portal
    # file transfer, where a browser holding a signed ticket uploads or
    # downloads directly — so production lists the suliko.ge origins here too.
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # ── App ─────────────────────────────────────────────────────────────────
    app_name: str = "Suliko Office API"

    #: Interface language a self-signed-up bureau starts in. Georgian, because
    #: that is who this is sold to; they can change it in Settings.
    default_signup_locale: str = "ka"
    #: Self sign-up (`POST /auth/signup`). Off: for now people join by an
    #: organisation's invitation, and anyone can open a personal account from
    #: the sign-in chooser once they have one (decided 2026-09-30).
    signup_enabled: bool = False
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

    @property
    def suliko_backend_enabled(self) -> bool:
        """Whether suliko.ge's backend is wired in (see `suliko_api_url`)."""
        return bool((self.suliko_api_url or "").strip())

    @property
    def email_configured(self) -> bool:
        """Whether auth mail can actually leave the building.

        A host and a From address are the minimum. Username and password are
        not required — an internal relay that authenticates by IP is a normal
        deployment.
        """
        return bool(self.smtp_host and self.smtp_from_email)

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
        if self.public_api and not self.bff_shared_secret.get_secret_value():
            problems.append(
                "PUBLIC_API is on but BFF_SHARED_SECRET is not set. An "
                "internet-facing API should not accept requests from callers "
                "other than the frontend."
            )
        if self.suliko_backend_enabled:
            if not self.suliko_api_url or not self.suliko_api_url.startswith("https://"):
                problems.append("SULIKO_API_URL must be https in production")
            if len(self.suliko_api_key.get_secret_value()) < 24:
                problems.append("SULIKO_API_KEY must be set (24+ characters) with SULIKO_API_URL")
        portal_secret = self.portal_shared_secret.get_secret_value()
        if portal_secret and len(portal_secret) < 32:
            # It signs statements about who a user is. A short key is a
            # brute-forceable key, and brute-forcing it is impersonation.
            problems.append("PORTAL_SHARED_SECRET must be at least 32 characters")
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

        if not self.email_configured:
            # Not merely a missing feature. `POST /auth/password/forgot`
            # answers 204 whether or not the account exists — it has to, or it
            # enumerates users — so with no mailer it tells every locked-out
            # person that their reset is on its way and then silently drops it.
            problems.append(
                "SMTP_HOST and SMTP_FROM_EMAIL are not both set. Password "
                "reset mail cannot be delivered, and the endpoint cannot tell "
                "the user that without also revealing which accounts exist."
            )
        if self.app_url.startswith("http://") and not self.app_url.startswith(
            ("http://localhost", "http://127.0.0.1")
        ):
            # Reset links are carried in email and clicked from anywhere.
            problems.append(f"APP_URL must be https in production: {self.app_url}")

        if problems:
            raise RuntimeError("Refusing to start in production:\n  - " + "\n  - ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
