"""Proof that a password was just accepted, carried to the organisation chooser.

Sign-in is two steps: email and password first, then "which organisation — or
your personal account?". Between them there is an account but no organisation,
so no session can exist yet. The ticket bridges the gap:

- HMAC-signed with a key derived from the master key, so it cannot be forged;
- valid for ten minutes;
- bound to the account's CURRENT password hash, so a password change (or a
  reset) kills every ticket issued before it.

It is not stored anywhere. It grants nothing but "pick one of your own
organisations", and every pick is re-checked against the account's
memberships at that moment.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime

from suliko.core.crypto import purpose_key

TTL_SECONDS = 600
_PURPOSE = "login-ticket/v1"


class TicketError(Exception):
    """Expired, forged, malformed, or for a password that has since changed."""


def _fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:24]


def _sign(body: str) -> str:
    digest = hmac.new(purpose_key(_PURPOSE), body.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _now(now: datetime | None) -> int:
    return int((now or datetime.now(UTC)).timestamp())


def issue(account_id: int, password_hash: str, *, now: datetime | None = None) -> str:
    body = f"{account_id}.{_now(now) + TTL_SECONDS}.{_fingerprint(password_hash)}"
    return f"{body}.{_sign(body)}"


def read(ticket: str, *, now: datetime | None = None) -> int:
    """The account id a genuine, unexpired ticket was issued for.

    Whether it was issued under the account's current password is `matches`,
    checked once the caller has loaded the account.
    """
    parts = ticket.split(".")
    if len(parts) != 4:
        raise TicketError("malformed")
    account, expires, fingerprint, signature = parts
    if not hmac.compare_digest(_sign(f"{account}.{expires}.{fingerprint}"), signature):
        raise TicketError("bad signature")
    try:
        account_id, expires_at = int(account), int(expires)
    except ValueError:
        raise TicketError("malformed") from None
    if expires_at < _now(now):
        raise TicketError("expired")
    return account_id


def matches(ticket: str, password_hash: str) -> bool:
    """Whether the ticket was issued under this password hash."""
    parts = ticket.split(".")
    return len(parts) == 4 and hmac.compare_digest(parts[2], _fingerprint(password_hash))


__all__ = ["TTL_SECONDS", "TicketError", "issue", "matches", "read"]
