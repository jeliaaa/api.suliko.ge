"""Staff users, sessions, MFA and password reset."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from suliko.db.base import Base, IdMixin, TenantScoped, TimestampMixin, enum_values


class Role(enum.StrEnum):
    """Five levels. See docs/03-SECURITY-AND-TENANCY.md §3.

    ``SUPERUSER`` is platform-level and is the one role that is not confined to
    its tenant row — see ``suliko.security.permissions``.
    """

    SUPERUSER = "superuser"
    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"
    STAFF = "staff"


class User(Base, IdMixin, TenantScoped, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        # Usernames and emails are unique *per tenant*, not globally: two
        # bureaus may each legitimately have an "admin" or the same shared
        # office address.
        UniqueConstraint("tenant_id", "username", name="uq_users_tenant_username"),
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        Index("ix_users_tenant_role", "tenant_id", "role"),
    )

    username: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Argon2id for everything written by this app. Legacy bcrypt hashes
    # imported from the PHP app verify too, and are re-hashed on first
    # successful login (suliko.security.passwords.verify_and_maybe_rehash).
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", values_callable=enum_values, native_enum=False, length=20),
        default=Role.STAFF,
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Set when the user must re-authenticate everywhere: password change,
    # role change, offboarding. Sessions older than this are rejected, which
    # revokes them without a delete sweep.
    sessions_invalid_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    def __repr__(self) -> str:
        return f"<User {self.id} {self.username!r} t{self.tenant_id} {self.role.value}>"


class MfaMethod(Base, IdMixin, TenantScoped, TimestampMixin):
    """A second factor enrolled by a user.

    Modelled as a table rather than columns on ``users`` so WebAuthn/passkeys
    can be added later without a schema change — the upgrade path in
    docs/03-SECURITY-AND-TENANCY.md §4.2.
    """

    __tablename__ = "mfa_methods"
    __table_args__ = (Index("ix_mfa_methods_tenant_user", "tenant_id", "user_id"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    method_type: Mapped[str] = mapped_column(String(20), default="totp", nullable=False)

    # Encrypted at rest with the tenant's data key. Never logged, never in a URL.
    secret_encrypted: Mapped[bytes] = mapped_column(nullable=False)

    # Activated only after the user proves one valid code, so a half-finished
    # enrolment cannot lock anyone out.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    # Replay protection: the last TOTP time-step accepted for this method.
    # Without it, a phished code stays valid for its whole 30-second window —
    # the step most TOTP implementations skip.
    last_used_timestep: Mapped[int | None] = mapped_column(Integer, default=None)

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None


class MfaRecoveryCode(Base, IdMixin, TenantScoped, TimestampMixin):
    """Single-use recovery codes, stored hashed exactly like passwords."""

    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (Index("ix_mfa_recovery_tenant_user", "tenant_id", "user_id"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class UserSession(Base, IdMixin, TenantScoped, TimestampMixin):
    """An opaque server-side session.

    Only the SHA-256 of the token is stored. A database read therefore does not
    yield a usable session token, the same property we want for API keys and
    password-reset tokens.
    """

    __tablename__ = "user_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
        Index("ix_user_sessions_tenant_user", "tenant_id", "user_id"),
        Index("ix_user_sessions_expires", "absolute_expires_at"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Two clocks: idle (slides on activity) and absolute (never extends).
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Null until the 2FA challenge is passed. A session with MFA pending can
    # ONLY call the challenge endpoint.
    mfa_satisfied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    ip: Mapped[str | None] = mapped_column(INET, default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)

    # Set for the duration of a superuser impersonation. Carried into every
    # audit entry written by this session.
    impersonated_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    impersonation_reason: Mapped[str | None] = mapped_column(String(500), default=None)

    def is_valid_at(self, now: datetime) -> bool:
        return (
            self.revoked_at is None
            and self.idle_expires_at > now
            and self.absolute_expires_at > now
        )


class PasswordResetToken(Base, IdMixin, TenantScoped, TimestampMixin):
    """Hashed, single-use, short-lived.

    The PHP app stores these raw, which makes a database read equivalent to
    account takeover. Storing the hash removes that.
    """

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_password_reset_token_hash"),
        Index("ix_password_reset_tenant_user", "tenant_id", "user_id"),
    )

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class LoginAttempt(Base, IdMixin, TimestampMixin):
    """Every attempt, successful or not.

    Deliberately NOT tenant-scoped: a failed login often cannot be attributed
    to a tenant (the username may not exist), and the platform needs to see
    credential-stuffing across tenants.
    """

    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_username_time", "username", "created_at"),
        Index("ix_login_attempts_ip_time", "ip", "created_at"),
    )

    username: Mapped[str] = mapped_column(String(100), nullable=False)
    ip: Mapped[str | None] = mapped_column(INET, default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(50), default=None)
