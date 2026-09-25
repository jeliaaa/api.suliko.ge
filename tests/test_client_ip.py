"""Who is calling: the address and user agent the limits and records use.

Every request reaches this API through the BFF, so the TCP peer is the BFF's
server. Before the BFF forwarded the browser's address, every per-IP limit was
shared by every user of every bureau — 20 failed logins anywhere locked out
everybody, and the platform accepted three sign-ups an hour in total. And an
unparsed forwarded value went straight into an `INET` column, so a proxy that
appends the port (IIS ARR) made every login a 500.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from starlette.datastructures import Headers

from suliko.api import deps
from suliko.api.v1 import auth


@pytest.mark.parametrize(
    ("raw", "parsed"),
    [
        ("203.0.113.5", "203.0.113.5"),
        ("203.0.113.5:51234", "203.0.113.5"),  # IIS ARR
        (" 203.0.113.5 ", "203.0.113.5"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("2001:db8::1", "2001:db8::1"),
        ("unknown", None),
        ("", None),
        ("<script>", None),
    ],
)
def test_addresses_are_parsed_before_they_reach_an_inet_column(
    raw: str, parsed: str | None
) -> None:
    assert deps._parse_ip(raw) == parsed


def _request(headers: dict[str, str], peer: str = "10.0.0.9") -> Any:
    # Real Starlette headers: case-insensitive, as in production.
    return SimpleNamespace(headers=Headers(headers), client=SimpleNamespace(host=peer))


@pytest.fixture
def gateway_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    secret = "a-shared-secret-for-tests-only-0123456789"
    settings = SimpleNamespace(bff_shared_secret=SecretStr(secret))
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    return secret


def test_the_bff_forwarded_address_wins_when_the_gateway_secret_matches(
    gateway_secret: str,
) -> None:
    request = _request(
        {
            "X-Suliko-Gateway": gateway_secret,
            "X-Suliko-Client-IP": "198.51.100.7",
            "X-Suliko-Client-UA": "Mozilla/5.0 (Browser)",
            "X-Forwarded-For": "34.1.2.3",  # the BFF's own egress address
        }
    )
    assert deps.get_client_ip(request) == "198.51.100.7"
    assert deps.get_client_user_agent(request) == "Mozilla/5.0 (Browser)"


def test_the_forwarded_address_is_ignored_without_the_secret(gateway_secret: str) -> None:
    """Anyone can send the header; only the BFF can send it with the secret."""
    request = _request(
        {
            "X-Suliko-Gateway": "wrong",
            "X-Suliko-Client-IP": "198.51.100.7",
            "X-Forwarded-For": "203.0.113.5:4431, 10.0.0.1",
        }
    )
    assert deps.get_client_ip(request) == "203.0.113.5"


def test_garbage_everywhere_falls_back_to_the_peer(gateway_secret: str) -> None:
    request = _request({"X-Forwarded-For": "not-an-ip"}, peer="10.0.0.9")
    assert deps.get_client_ip(request) == "10.0.0.9"


def test_sign_in_ignores_the_case_of_the_organisation_and_username() -> None:
    """Slugs are stored lower-case and usernames are email addresses."""
    source = inspect.getsource(auth.login)
    assert "payload.tenant_slug.strip().lower()" in source
    assert "func.lower(User.username) == username.lower()" in source
    forgot = inspect.getsource(auth.forgot_password)
    assert "payload.tenant_slug.strip().lower()" in forgot


def test_the_reset_email_is_sent_after_the_response() -> None:
    """Inline SMTP made a known account measurably slower than an unknown one."""
    source = inspect.getsource(auth.forgot_password)
    assert "background.add_task(mail.send" in source
    assert "await mail.send" not in source


def test_a_self_service_reset_clears_the_forced_change() -> None:
    assert "must_change_password = False" in inspect.getsource(auth.reset_password)
