"""Password-reset tokens: issue, and spend exactly once.

The ``password_reset_tokens`` table has existed since revision 0001 and until
now nothing wrote to it. This is the code it was waiting for.

## The shape

Same as sessions and recovery codes: a 256-bit CSPRNG token is generated, the
SHA-256 of it is stored, and the plaintext is returned once and never
recoverable. A database read therefore does not yield a usable reset link —
which is the specific bug in the PHP app, where these are stored raw and
reading the table is equivalent to account takeover.

`hash_token` rather than Argon2 on purpose: the input is 256 bits of entropy,
not a human-chosen password, so there is nothing to brute-force and no reason
to pay a KDF on the verification path.

## Why issuing invalidates the previous ones

Requesting a second link must kill the first. Otherwise every request a user
makes while flailing at a login screen leaves another live key to their
account sitting in their inbox for an hour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from suliko.db.session import bind_tenant_guc
from suliko.db.tenancy import bypass_tenant_scope, tenant_scope
from suliko.models.user import PasswordResetToken, User
from suliko.security.passwords import generate_token, hash_token


def _aware(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    PostgreSQL hands back an aware datetime for ``TIMESTAMPTZ``; SQLite, which
    the tests run on, drops the offset. Everything written here is UTC, so
    reattaching it is a restatement rather than an assumption — and comparing
    a naive value against an aware one raises instead of returning False,
    which would turn a portability detail into a 500 on the reset path.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def issue(db: AsyncSession, user: User, *, ttl_seconds: int) -> str:
    """Mint a reset token for ``user`` and return the plaintext, once.

    The caller is responsible for committing.
    """
    now = datetime.now(UTC)

    # Spend every outstanding token for this user, so only the newest link
    # works. Stamped rather than deleted: `used_at` is the audit trail of a
    # link having existed at all.
    with bypass_tenant_scope():
        outstanding = (
            (
                await db.execute(
                    select(PasswordResetToken).where(
                        PasswordResetToken.user_id == user.id,
                        PasswordResetToken.used_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in outstanding:
            row.used_at = now

    token = generate_token("rst_")

    await bind_tenant_guc(db, user.tenant_id)
    with tenant_scope(user.tenant_id):
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
        )
        await db.flush()

    return token


async def consume(db: AsyncSession, token: str) -> User | None:
    """Spend a token and return the user it belongs to, or None.

    None covers every failure identically — unknown, already used, expired,
    or belonging to a deactivated user. The caller cannot tell them apart and
    must not: distinguishing "expired" from "never existed" tells an attacker
    holding a stolen link whether it was ever real.

    Spending happens here, before the caller sets the password, so a token can
    never be replayed even if the write that follows fails. Callers therefore
    reject anything they can reject — a too-weak password, most obviously —
    BEFORE calling this, or a typo costs the user another email.
    """
    now = datetime.now(UTC)

    with bypass_tenant_scope():
        row = (
            await db.execute(
                select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(token))
            )
        ).scalar_one_or_none()

        if row is None or row.used_at is not None or _aware(row.expires_at) <= now:
            return None

        user = await db.get(User, row.user_id)
        if user is None or not user.is_active:
            return None

        row.used_at = now
        await db.flush()

    return user


__all__ = ["consume", "issue"]
