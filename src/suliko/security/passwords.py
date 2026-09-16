"""Password hashing and verification.

Argon2id for everything this app writes. bcrypt verification is retained only
so users migrated from the PHP app can sign in once with their existing
password, at which point the hash is transparently upgraded.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from contextlib import suppress

import bcrypt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# OWASP-recommended Argon2id parameters (64 MiB, t=3, p=4). Raising memory is
# the most effective lever against GPU cracking; 64 MiB is affordable for a
# login-rate workload and is the current baseline recommendation.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

#: Verified when the username does not exist, so a missing account and a wrong
#: password take the same time. Without this the response time alone
#: enumerates valid usernames.
_DUMMY_HASH = _hasher.hash("dummy-password-for-constant-time-comparison")

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _is_bcrypt(hashed: str) -> bool:
    return hashed.startswith(("$2a$", "$2b$", "$2y$"))


def verify_password(password: str, hashed: str) -> bool:
    """Verify against either an Argon2id or a legacy bcrypt hash."""
    if _is_bcrypt(hashed):
        try:
            # bcrypt silently truncates at 72 bytes. PHP's password_hash did
            # the same, so a legacy password longer than that still verifies
            # exactly as it did before — matching the old behaviour is the
            # point here, not fixing it.
            return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode("utf-8"))
        except (ValueError, TypeError):
            return False

    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """True for legacy bcrypt hashes, and for Argon2 hashes below current params."""
    if _is_bcrypt(hashed):
        return True
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return True


def verify_and_maybe_rehash(password: str, hashed: str) -> tuple[bool, str | None]:
    """Verify, and return a replacement hash when the stored one is outdated.

    Returns ``(ok, new_hash_or_None)``. The caller persists ``new_hash`` when
    it is not None. This is the whole bcrypt-to-Argon2id migration: no bulk
    re-hash, no forced password reset, users upgrade as they sign in.
    """
    if not verify_password(password, hashed):
        return False, None
    if needs_rehash(hashed):
        return True, hash_password(password)
    return True, None


def waste_time_verifying() -> None:
    """Burn one hash verification against a dummy.

    Call on the "user not found" branch so it costs the same as a real check.
    """
    with suppress(VerifyMismatchError, VerificationError, InvalidHashError):
        _hasher.verify(_DUMMY_HASH, "definitely-not-the-password")


# ── Opaque tokens (sessions, reset tokens, API keys) ─────────────────────────


def generate_token(prefix: str = "") -> str:
    """A 256-bit URL-safe token."""
    token = secrets.token_urlsafe(32)
    return f"{prefix}{token}" if prefix else token


#: Unambiguous when read aloud or copied from an email: no O/0, I/l/1, U/V.
#: A one-time password gets typed by hand from a message, often on a phone,
#: and a character someone has to guess at is a support call.
_OTP_ALPHABET = "ABCDEFGHJKMNPQRSTWXYZabcdefghijkmnpqrstwxyz23456789"


def generate_one_time_password(length: int = 16) -> str:
    """A temporary password for an invited user.

    Sixteen characters from a 51-character alphabet is ~90 bits, which is far
    past anything guessable — and it has to be, because this value travels
    through email and is valid until the user replaces it.

    Formatted in groups of four. It exists to be transcribed once, and a
    single 16-character run is where transcription errors come from.
    """
    raw = "".join(secrets.choice(_OTP_ALPHABET) for _ in range(length))
    return "-".join(raw[i : i + 4] for i in range(0, length, 4))


def hash_token(token: str) -> str:
    """SHA-256 of an opaque token, for storage.

    Plain SHA-256 rather than Argon2 is correct here and only here: these
    tokens are 256 bits of CSPRNG output, so there is no guessable input to
    slow down, and session lookup happens on every single request. Passwords
    are low-entropy and get Argon2; random tokens do not need it.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def validate_password_strength(password: str) -> list[str]:
    """Length only, plus a trivial-pattern check.

    No composition rules and no forced rotation: both are known to produce
    worse passwords. The real defence is the breached-password check below.
    """
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if password and password.lower() in _OBVIOUS:
        problems.append("That password is too common.")
    return problems


_OBVIOUS = frozenset(
    {
        "password",
        "password123",
        "123456789012",
        "qwertyuiop12",
        "administrator",
        "letmein12345",
    }
)


def breached_password_prefix(password: str) -> tuple[str, str]:
    """Split the SHA-1 of a password for a k-anonymity range query.

    Returns ``(first_5_hex, remaining_hex)``. Send only the prefix to the
    Have-I-Been-Pwned range API and match the suffix locally — the password,
    and even its full hash, never leaves this process.

    SHA-1 is required by that API's protocol; it is not used to protect
    anything here.
    """
    digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
    return digest[:5], digest[5:]
