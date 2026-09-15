"""Portal tokens: the statement "this is suliko.ge user X" must be unforgeable.

Everything the portal lets a translator see follows from the user id in these
tokens, so each way of producing a token that verifies without the secret — or
of stretching a valid one past its purpose — gets a test.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from suliko.security.portal_tokens import PortalTokenError, sign_token, verify_token

SECRET = "portal-secret-for-tests-0123456789abcdef"
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _assertion(**overrides: object) -> str:
    params: dict[str, object] = {
        "kind": "assertion",
        "user_id": "3f2b8c1e-0000-4000-8000-000000000001",
        "ttl_seconds": 60,
        "now": NOW,
    }
    params.update(overrides)
    return sign_token(SECRET, **params)  # type: ignore[arg-type]


def _verify(token: str, **overrides: object):  # type: ignore[no-untyped-def]
    params: dict[str, object] = {
        "expected_type": "assertion",
        "max_age_seconds": 60,
        "now": NOW,
    }
    params.update(overrides)
    return verify_token(SECRET, token, **params)  # type: ignore[arg-type]


def _reencode(token: str, **claim_changes: object) -> str:
    """Tamper with the claims but keep the original signature."""
    version, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims.update(claim_changes)
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{version}.{forged}.{signature}"


def test_round_trip() -> None:
    claims = _verify(_assertion(is_admin=True))
    assert claims.user_id == "3f2b8c1e-0000-4000-8000-000000000001"
    assert claims.is_admin is True
    assert claims.method is None


def test_wrong_secret_is_rejected() -> None:
    token = _assertion()
    with pytest.raises(PortalTokenError):
        verify_token(
            "another-secret-entirely-0123456789",
            token,
            expected_type="assertion",
            max_age_seconds=60,
            now=NOW,
        )


def test_tampered_user_id_is_rejected() -> None:
    """The attack that matters: keep a valid signature, swap in someone else."""
    with pytest.raises(PortalTokenError, match="signature"):
        _verify(_reencode(_assertion(), sub="someone-else"))


def test_granting_yourself_admin_is_rejected() -> None:
    with pytest.raises(PortalTokenError, match="signature"):
        _verify(_reencode(_assertion(), adm=True))


def test_expired_token_is_rejected() -> None:
    token = _assertion()
    with pytest.raises(PortalTokenError, match="expired"):
        _verify(token, now=NOW + timedelta(seconds=61))


def test_token_from_the_future_is_rejected() -> None:
    token = _assertion(now=NOW + timedelta(minutes=5))
    with pytest.raises(PortalTokenError, match="future"):
        _verify(token, max_age_seconds=3600)


def test_small_clock_skew_is_tolerated() -> None:
    _verify(_assertion(now=NOW + timedelta(seconds=10)))


def test_overlong_lifetime_is_rejected() -> None:
    """Even correctly signed: a week-long assertion is a mistake to refuse."""
    with pytest.raises(PortalTokenError, match="lifetime"):
        _verify(_assertion(ttl_seconds=7 * 24 * 3600))


def test_ticket_is_not_accepted_as_an_assertion() -> None:
    """A ticket lives in a browser URL. It must never work server-to-server."""
    ticket = sign_token(
        SECRET,
        kind="ticket",
        user_id="u",
        ttl_seconds=60,
        method="GET",
        path="/api/v1/portal/personal-orders/1/files/1",
        now=NOW,
    )
    with pytest.raises(PortalTokenError, match="expected a assertion"):
        _verify(ticket)


def test_assertion_is_not_accepted_as_a_ticket() -> None:
    with pytest.raises(PortalTokenError, match="expected a ticket"):
        _verify(_assertion(), expected_type="ticket")


def test_ticket_carries_its_request_and_never_admin() -> None:
    ticket = sign_token(
        SECRET,
        kind="ticket",
        user_id="u",
        ttl_seconds=300,
        is_admin=True,
        method="post",
        path="/api/v1/portal/personal-orders/7/files",
        now=NOW,
    )
    claims = _verify(ticket, expected_type="ticket", max_age_seconds=300)
    assert claims.method == "POST"
    assert claims.path == "/api/v1/portal/personal-orders/7/files"
    assert claims.is_admin is False


def test_ticket_needs_a_method_and_path() -> None:
    with pytest.raises(PortalTokenError):
        sign_token(SECRET, kind="ticket", user_id="u", ttl_seconds=60)


@pytest.mark.parametrize(
    "token",
    ["", "v1", "v1.abc", "v2.abc.def", "v1.not-base64!.sig", "v1..", "a.b.c.d"],
)
def test_malformed_tokens_are_rejected(token: str) -> None:
    with pytest.raises(PortalTokenError):
        _verify(token)


def test_empty_secret_never_verifies() -> None:
    """An unset secret must not turn into "any HMAC of the empty key"."""
    with pytest.raises(PortalTokenError):
        sign_token("", kind="assertion", user_id="u", ttl_seconds=60)
    with pytest.raises(PortalTokenError):
        verify_token("", _assertion(), expected_type="assertion", max_age_seconds=60, now=NOW)


def test_boolean_timestamps_are_rejected() -> None:
    """`true` is an int in Python; it must not pass as a timestamp."""
    import hashlib
    import hmac

    claims = {"typ": "assertion", "sub": "u", "adm": False, "iat": True, "exp": True}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    signing_input = f"v1.{payload}"
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(PortalTokenError, match="integer"):
        _verify(f"{signing_input}.{signature}")
