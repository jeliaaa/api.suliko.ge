"""Slow requests are logged with the time spent on outgoing calls."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import structlog
from fastapi import FastAPI

from suliko.config import get_settings
from suliko.core.slow_requests import SlowRequestMiddleware, upstream_hooks


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SlowRequestMiddleware)
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
        event_hooks=upstream_hooks("suliko_backend"),
    )

    @app.get("/orders/{order_id}")
    async def slow(order_id: int) -> dict[str, int]:
        await upstream.get("http://upstream.test/x")
        await asyncio.sleep(0.05)
        return {"id": order_id}

    @app.get("/quick")
    async def quick() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
def threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("SLOW_REQUEST_SECONDS", "0.03")
    yield
    get_settings.cache_clear()


async def _get(path: str) -> int:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return (await client.get(path)).status_code


async def test_slow_request_is_logged_with_route_and_upstream(threshold: None) -> None:
    with structlog.testing.capture_logs() as logs:
        assert await _get("/orders/123456") == 200
    [entry] = [e for e in logs if e["event"] == "slow_request"]
    assert entry["path"] == "/orders/{order_id}"
    assert entry["status"] == 200
    assert entry["duration_ms"] >= 50
    assert set(entry["upstream"]) == {"suliko_backend"}
    assert "123456" not in str(entry)


async def test_fast_request_is_not_logged(threshold: None) -> None:
    with structlog.testing.capture_logs() as logs:
        assert await _get("/quick") == 200
    assert not [e for e in logs if e["event"] == "slow_request"]


async def test_zero_switches_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("SLOW_REQUEST_SECONDS", "0")
    try:
        with structlog.testing.capture_logs() as logs:
            assert await _get("/orders/1") == 200
        assert not logs
    finally:
        get_settings.cache_clear()
