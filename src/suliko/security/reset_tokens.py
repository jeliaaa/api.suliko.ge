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
account sitting in their inbox for an hour. Scoped to tokens of the SAME
``purpose`` — see below — so unrelated errands do not clobber each other.

## Purposes, one table

Three things end up minting a row here — a forgot-password request, an
invite's set-password link, and now a signup's email-confirmation link — and
all of them are really the same proof, "this address received something we
sent it". The first two are close enough (both end at `reset_password`
setting a new one) that they share the ``password_reset`` purpose; email
verification is a distinct errand with a distinct consumer, so it gets its
own. A row's purpose is checked on issue (which outstanding tokens it may
retire) and on consume (which token a caller's link may possibly be).
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


#: Default purpose: a link that proves nothing but "I received mail sent to
#: this address". A forgot-password request and an invite's set-password link
#: are the same proof, so they share this one rather than each getting their
#: own string to keep in sync.
PASSWORD_RESET = "password_reset"  # noqa: S105 -- a token *purpose* label, not a secret
EMAIL_VERIFICATION = "email_verification"
#: An organisation's invitation to someone who already has an account.
INVITATION = "invitation"


async def issue(
    db: AsyncSession, user: User, *, ttl_seconds: int, purpose: str = PASSWORD_RESET
) -> str:
    """Mint a token for ``user`` and return the plaintext, once.

    The caller is responsible for committing.
    """
    now = datetime.now(UTC)

    # Spend every outstanding token of the SAME purpose for this user, so only
    # the newest link of that kind works. A verification link being issued
    # must not silently kill an unrelated outstanding password-reset link, or
    # the reverse — the two are unrelated errands that happen to share a
    # table. Stamped rather than deleted: `used_at` is the audit trail of a
    # link having existed at all.
    with bypass_tenant_scope():
        outstanding = (
            (
                await db.execute(
                    select(PasswordResetToken).where(
                        PasswordResetToken.user_id == user.id,
                        PasswordResetToken.purpose == purpose,
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
                purpose=purpose,
            )
        )
        await db.flush()

    return token


async def consume(db: AsyncSession, token: str, *, purpose: str = PASSWORD_RESET) -> User | None:
    """Spend a token of ``purpose`` and return the user it belongs to, or None.

    None covers every failure identically — unknown, already used, expired,
    belonging to a deactivated user, or minted for a DIFFERENT purpose (a
    password-reset link handed to the email-verification endpoint is just as
    invalid as a forged one). The caller cannot tell these apart and must not:
    distinguishing "expired" from "never existed" tells an attacker holding a
    stolen link whether it was ever real.

    Spending happens here, before the caller acts on it, so a token can never
    be replayed even if the write that follows fails. Callers therefore reject
    anything they can reject — a too-weak password, most obviously — BEFORE
    calling this, or a typo costs the user another email.
    """
    now = datetime.now(UTC)

    with bypass_tenant_scope():
        row = (
            await db.execute(
                select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(token))
            )
        ).scalar_one_or_none()

        if (
            row is None
            or row.purpose != purpose
            or row.used_at is not None
            or _aware(row.expires_at) <= now
        ):
            return None

        user = await db.get(User, row.user_id)
        if user is None or not user.is_active:
            return None

        row.used_at = now
        await db.flush()

    return user


__all__ = ["EMAIL_VERIFICATION", "INVITATION", "PASSWORD_RESET", "consume", "issue"]
