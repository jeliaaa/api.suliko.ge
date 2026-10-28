"""Log requests that took long, and how much of that was waiting on others.

A request that is slow is slow for one of a few reasons: our own database
work, a wait on suliko.ge (sign-in, translation) or the Word editor service,
or a queue behind other work in the same process. The first thing needed to
tell them apart is a record of WHICH requests were slow and how long they sat
in an outgoing call. This module writes that record, one ``slow_request`` log
line per request over ``SLOW_REQUEST_SECONDS``.

The upstream time is the time until the other side's response HEADERS arrived,
summed over the request's outgoing calls. A call that timed out never gets
that far, so it is not counted here; it shows as a long request with an
error line next to it (``suliko_backend_unreachable`` and its siblings).

Pure ASGI rather than ``BaseHTTPMiddleware``: the latter runs the app in
another task, and the per-request record has to be visible to the outgoing
calls made while handling it.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any

import httpx
import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from suliko.config import get_settings

log = structlog.get_logger()

#: The outgoing calls of the request being handled: (service, milliseconds).
_upstream: ContextVar[list[tuple[str, float]] | None] = ContextVar("upstream_calls", default=None)

_STARTED_KEY = "suliko_started_at"


async def _on_request(request: httpx.Request) -> None:
    request.extensions[_STARTED_KEY] = time.perf_counter()


def upstream_hooks(service: str) -> dict[str, list[Any]]:
    """``event_hooks`` for an ``httpx.AsyncClient`` that talks to ``service``."""

    async def on_response(response: httpx.Response) -> None:
        started = response.request.extensions.get(_STARTED_KEY)
        calls = _upstream.get()
        if started is not None and calls is not None:
            calls.append((service, (time.perf_counter() - started) * 1000))

    return {"request": [_on_request], "response": [on_response]}


class SlowRequestMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        threshold = get_settings().slow_request_seconds
        if scope["type"] != "http" or threshold <= 0:
            await self.app(scope, receive, send)
            return

        calls: list[tuple[str, float]] = []
        token = _upstream.set(calls)
        started = time.perf_counter()
        status = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _upstream.reset(token)
            elapsed = time.perf_counter() - started
            if elapsed >= threshold:
                route = scope.get("route")
                by_service: dict[str, float] = {}
                for service, ms in calls:
                    by_service[service] = by_service.get(service, 0.0) + ms
                log.warning(
                    "slow_request",
                    method=scope["method"],
                    # The route template, not the URL: no ids or search text
                    # in the log, and every order's page groups together.
                    path=getattr(route, "path", None) or "unmatched",
                    status=status,
                    duration_ms=round(elapsed * 1000),
                    upstream_ms=round(sum(by_service.values())),
                    upstream={name: round(ms) for name, ms in by_service.items()},
                )
