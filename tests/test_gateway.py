"""The BFF shared-secret gateway.

Exercised through the real app so the middleware ORDER is covered too — the
gateway must run before anything that touches the database, or an unknown
caller still costs a connection.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from suliko.config import get_settings
from suliko.core.gateway import HEADER_NAME

SECRET = "gateway-secret-for-tests"


@pytest.fixture
def client_with_gateway() -> Iterator[TestClient]:
    """An app instance with the gateway enabled.

    Settings are an lru_cache'd singleton, so the cache is cleared around the
    fixture rather than mutated in place — otherwise the override leaks into
    every later test in the session.
    """
    get_settings.cache_clear()
    settings = get_settings()
    original = settings.bff_shared_secret

    from pydantic import SecretStr

    object.__setattr__(settings, "bff_shared_secret", SecretStr(SECRET))

    from suliko.main import create_app

    with TestClient(create_app()) as client:
        yield client

    object.__setattr__(settings, "bff_shared_secret", original)
    get_settings.cache_clear()


@pytest.fixture
def client_without_gateway() -> Iterator[TestClient]:
    get_settings.cache_clear()
    from suliko.main import create_app

    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_health_is_reachable_without_the_secret(client_with_gateway: TestClient) -> None:
    """Uptime monitors and load balancers must be able to probe it."""
    response = client_with_gateway.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_request_without_the_header_is_rejected(client_with_gateway: TestClient) -> None:
    response = client_with_gateway.get("/api/v1/clients")
    assert response.status_code == 404


def test_request_with_a_wrong_secret_is_rejected(client_with_gateway: TestClient) -> None:
    response = client_with_gateway.get("/api/v1/clients", headers={HEADER_NAME: "not-the-secret"})
    assert response.status_code == 404


def test_rejection_is_404_not_401(client_with_gateway: TestClient) -> None:
    """404 rather than 401/403 on purpose: a scanner should learn nothing
    about what lives here, including whether the path exists."""
    response = client_with_gateway.get("/api/v1/clients")
    assert response.status_code == 404
    assert "gateway" not in response.text.lower()
    assert "secret" not in response.text.lower()


def test_correct_secret_passes_through_to_normal_auth(
    client_with_gateway: TestClient,
) -> None:
    """With the right secret the request reaches the app and is then rejected
    for the real reason — no session. The gateway is not authentication."""
    response = client_with_gateway.get("/api/v1/clients", headers={HEADER_NAME: SECRET})
    assert response.status_code == 401
    assert "bearer" in response.json()["detail"].lower()


def test_preflight_is_exempt(client_with_gateway: TestClient) -> None:
    """Browsers do not send custom headers on a CORS preflight, so blocking
    OPTIONS would break the frontend before it ever sent a real request."""
    response = client_with_gateway.options(
        "/api/v1/clients",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code < 400


def test_gateway_is_inert_when_no_secret_is_configured(
    client_without_gateway: TestClient,
) -> None:
    """Loopback-only deployments and local development need no gateway."""
    response = client_without_gateway.get("/api/v1/clients")
    # Reaches the auth layer rather than being gated away.
    assert response.status_code == 401


# ── Portal file tickets ─────────────────────────────────────────────────────


def test_portal_ticket_request_passes_the_gateway(client_with_gateway: TestClient) -> None:
    """A browser downloading a file cannot hold the gateway secret; its ticket
    is checked by the endpoint instead — so a bogus one gets 401, not 404."""
    response = client_with_gateway.get(
        "/api/v1/portal/personal-orders/1/files/1", params={"ticket": "not-a-real-ticket"}
    )
    assert response.status_code == 401


def test_portal_request_without_a_ticket_is_still_gated(client_with_gateway: TestClient) -> None:
    assert client_with_gateway.get("/api/v1/portal/me").status_code == 404


def test_a_ticket_parameter_does_not_open_other_routes(client_with_gateway: TestClient) -> None:
    response = client_with_gateway.get("/api/v1/clients", params={"ticket": "anything"})
    assert response.status_code == 404


def test_short_portal_secret_is_refused_in_production() -> None:
    with pytest.raises(RuntimeError, match="PORTAL_SHARED_SECRET"):
        _prod(portal_shared_secret="too-short").validate_for_production()


# ── Production validation ───────────────────────────────────────────────────


def _prod(**overrides: object):  # type: ignore[no-untyped-def]
    from suliko.config import Settings

    base: dict[str, object] = {
        "environment": "production",
        "debug": False,
        "db_echo": False,
        "encryption_master_key": "a" * 44,
        "redis_url": "redis://localhost:6379/0",
        "cors_origins": ["https://app.suliko.ge"],
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_public_api_requires_the_gateway_secret() -> None:
    with pytest.raises(RuntimeError, match="BFF_SHARED_SECRET"):
        _prod(public_api=True).validate_for_production()


def test_public_api_with_the_secret_is_accepted() -> None:
    _prod(public_api=True, bff_shared_secret=SECRET).validate_for_production()


def test_loopback_deployment_does_not_require_the_secret() -> None:
    _prod(public_api=False).validate_for_production()
