"""TOTP two-factor authentication.

RFC 6238, 30-second step, 6 digits, SHA-1 — SHA-1 because every authenticator
app supports it and most support nothing else. It is a HMAC construction here,
where SHA-1 is not broken; this is not the same as using SHA-1 for signatures.

SMS is deliberately not offered as a second factor: SIM swap is the standard
attack, and this app already sends customer notifications over SMS, so a
compromised SMS channel would be doubly damaging.

Reference: docs/03-SECURITY-AND-TENANCY.md §4.2.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import pyotp

TOTP_INTERVAL = 30
TOTP_DIGITS = 6

#: Accept the previous and next step as well as the current one, to tolerate
#: clock drift. One step each way is ~90 seconds of total validity — wider
#: windows meaningfully help an attacker who has phished a code.
TOTP_VALID_WINDOW = 1

RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_BYTES = 5  # 10 hex chars, formatted as two groups of five.


class ReplayError(Exception):
    """A code from an already-used time step was presented again.

    This is the check most TOTP implementations omit. Without it a phished or
    shoulder-surfed code stays valid for its entire window, which is exactly
    the window a real-time phishing proxy operates in.
    """


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_name: str, issuer: str) -> str:
    """otpauth:// URI for the enrolment QR code.

    Never log this and never put it in a URL path or query string — it
    contains the shared secret in full.
    """
    return pyotp.TOTP(secret, interval=TOTP_INTERVAL, digits=TOTP_DIGITS).provisioning_uri(
        name=account_name, issuer_name=issuer
    )


def current_timestep(at: float | None = None) -> int:
    return int((at if at is not None else time.time()) // TOTP_INTERVAL)


@dataclass(frozen=True, slots=True)
class TotpVerification:
    ok: bool
    #: The step the code belonged to. Persist it as ``last_used_timestep`` so
    #: the same code cannot be replayed.
    timestep: int | None = None


def verify_code(
    secret: str,
    code: str,
    last_used_timestep: int | None = None,
    at: float | None = None,
) -> TotpVerification:
    """Verify a TOTP code, rejecting replays of an already-used step.

    Raises ``ReplayError`` when the code is otherwise valid but its step has
    already been consumed — a distinct signal from "wrong code", because it
    means someone is presenting a code the legitimate user already used.
    """
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return TotpVerification(ok=False)

    now = at if at is not None else time.time()
    totp = pyotp.TOTP(secret, interval=TOTP_INTERVAL, digits=TOTP_DIGITS)

    # Identify which step within the window the code matches, so it can be
    # recorded. pyotp's own verify() only returns a boolean.
    for offset in range(-TOTP_VALID_WINDOW, TOTP_VALID_WINDOW + 1):
        candidate_time = now + (offset * TOTP_INTERVAL)
        if secrets.compare_digest(totp.at(int(candidate_time)), code):
            step = current_timestep(candidate_time)
            if last_used_timestep is not None and step <= last_used_timestep:
                raise ReplayError(f"TOTP step {step} has already been used.")
            return TotpVerification(ok=True, timestep=step)

    return TotpVerification(ok=False)


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Single-use codes, shown once at enrolment and then stored hashed.

    Formatted ``abcde-fghij`` — grouped so they can be read aloud or copied
    off paper without transcription errors.
    """
    codes: list[str] = []
    for _ in range(count):
        raw = secrets.token_hex(RECOVERY_CODE_BYTES)
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalise_recovery_code(code: str) -> str:
    return code.strip().lower().replace(" ", "")
