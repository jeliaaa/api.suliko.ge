"""Signed statements from the suliko.ge server about who is acting.

The translator portal has no login of its own. A translator signs in to
suliko.ge, whose .NET backend issues their token; the suliko.ge Next.js server
checks that token and then tells this API which suliko.ge user is calling. This
module is what makes that statement trustworthy.

Two token types share one format:

- **assertion** — sent server-to-server with every portal call: "this request is
  from suliko.ge user X, who is / is not a suliko.ge admin". It lives for about
  a minute, long enough for one request and too short to be worth stealing.

- **ticket** — handed to a browser so it can upload or download ONE file
  directly. Vercel caps a function's request and response bodies at 4.5 MB, so
  document scans cannot travel through the Next.js server. A ticket is bound to
  one HTTP method and one path and lives for a few minutes: the same shape as a
  presigned storage URL.

Format: ``v1.<base64url(json claims)>.<base64url(HMAC-SHA256)>``. HMAC rather
than a JWT library because both ends are ours and the claims are fixed; a
single fixed format leaves no ``alg`` header to confuse.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

TokenType = Literal["assertion", "ticket"]

VERSION = "v1"

#: Tolerated difference between the suliko.ge server's clock and ours.
CLOCK_SKEW_SECONDS = 30

#: ASP.NET Identity ids are GUIDs; the column holding them is 450 wide.
MAX_USER_ID_LENGTH = 450


class PortalTokenError(Exception):
    """Malformed, forged, expired, or presented for the wrong purpose.

    The message is for logs. Callers answer every variant with the same flat
    401, so a forger learns nothing about which check failed.
    """


@dataclass(frozen=True, slots=True)
class PortalClaims:
    type: TokenType
    user_id: str
    is_admin: bool
    #: Tickets only: the one request this token may authorise.
    method: str | None
    path: str | None
    issued_at: int
    expires_at: int


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(secret: str, signing_input: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
    return _b64encode(digest.digest())


def _now(now: datetime | None) -> int:
    return int((now or datetime.now(UTC)).timestamp())


def sign_token(
    secret: str,
    *,
    kind: TokenType,
    user_id: str,
    ttl_seconds: int,
    is_admin: bool = False,
    method: str | None = None,
    path: str | None = None,
    now: datetime | None = None,
) -> str:
    """Produce a token.

    In production the suliko.ge server signs; this function exists so tests
    can, and so the format has one reference implementation next to the
    verifier. ``src/lib/crm`` in suliko-front must produce the same bytes.
    """
    if not secret:
        raise PortalTokenError("no portal secret configured")
    if kind == "ticket" and (not method or not path):
        raise PortalTokenError("a ticket needs a method and a path")

    issued_at = _now(now)
    claims: dict[str, Any] = {
        "typ": kind,
        "sub": user_id,
        "adm": is_admin,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    if kind == "ticket":
        claims["mth"] = (method or "").upper()
        claims["pth"] = path

    payload = _b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{VERSION}.{payload}"
    return f"{signing_input}.{_signature(secret, signing_input)}"


def _require_int(claims: dict[str, Any], key: str) -> int:
    value = claims.get(key)
    # `bool` is a subclass of `int`; `"iat": true` must not pass as a timestamp.
    if type(value) is not int:
        raise PortalTokenError(f"claim {key!r} is not an integer")
    return value


def _require_str(claims: dict[str, Any], key: str, max_length: int = 2048) -> str:
    value = claims.get(key)
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise PortalTokenError(f"claim {key!r} is missing or malformed")
    return value


def verify_token(
    secret: str,
    token: str,
    *,
    expected_type: TokenType,
    max_age_seconds: int,
    now: datetime | None = None,
) -> PortalClaims:
    """Check a token and return its claims, or raise ``PortalTokenError``.

    The signature is compared before the payload is parsed, so nothing an
    attacker wrote is interpreted until it is known to be ours.
    """
    if not secret:
        raise PortalTokenError("no portal secret configured")

    parts = token.split(".")
    if len(parts) != 3 or parts[0] != VERSION:
        raise PortalTokenError("not a v1 portal token")

    expected = _signature(secret, f"{parts[0]}.{parts[1]}")
    # Constant-time: a naive == leaks the signature one byte at a time.
    if not hmac.compare_digest(expected, parts[2]):
        raise PortalTokenError("bad signature")

    try:
        claims = json.loads(_b64decode(parts[1]))
    except (ValueError, binascii.Error) as exc:
        raise PortalTokenError("unreadable payload") from exc
    if not isinstance(claims, dict):
        raise PortalTokenError("payload is not an object")

    if claims.get("typ") != expected_type:
        # An assertion must not open a file, and a ticket — which a browser
        # holds — must never be accepted as a server-to-server assertion.
        raise PortalTokenError(f"expected a {expected_type}, got {claims.get('typ')!r}")

    user_id = _require_str(claims, "sub", MAX_USER_ID_LENGTH)
    is_admin = claims.get("adm", False)
    if not isinstance(is_admin, bool):
        raise PortalTokenError("claim 'adm' is not a boolean")

    issued_at = _require_int(claims, "iat")
    expires_at = _require_int(claims, "exp")
    current = _now(now)

    if expires_at <= current:
        raise PortalTokenError("expired")
    if issued_at > current + CLOCK_SKEW_SECONDS:
        raise PortalTokenError("issued in the future")
    if expires_at - issued_at > max_age_seconds:
        # The signer is trusted, but not to mint week-long tokens by mistake.
        raise PortalTokenError("lifetime exceeds the allowed maximum")

    method: str | None = None
    path: str | None = None
    if expected_type == "ticket":
        method = _require_str(claims, "mth", 10)
        path = _require_str(claims, "pth")

    return PortalClaims(
        type=expected_type,
        user_id=user_id,
        # Only an assertion can carry admin rights; a ticket never does.
        is_admin=is_admin if expected_type == "assertion" else False,
        method=method,
        path=path,
        issued_at=issued_at,
        expires_at=expires_at,
    )
