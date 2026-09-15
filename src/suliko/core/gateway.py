"""Shared-secret gate between the Vercel BFF and this API.

## Why this exists

When the frontend runs on Vercel, this API has to be reachable from the public
internet — Vercel's servers call it, and Vercel does not publish stable egress
IP ranges that could be allow-listed on every plan. So the API is exposed, but
it has exactly **one** legitimate caller.

A shared secret in a header closes the gap between "anyone on the internet can
reach /auth/login" and "only our frontend can". It is defence in depth, not
authentication: every endpoint still enforces sessions, permissions and
tenancy behind it. Losing the secret does not grant access to anything; it
only puts an attacker back where they would have been without it.

## What it does not do

It does not protect against a compromised Vercel deployment, and it does not
replace rate limiting — both still matter.

## Optional by design

Leave ``BFF_SHARED_SECRET`` unset and this middleware does nothing. That is
correct for local development and for a loopback-only deployment where the API
is not internet-facing.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from suliko.config import get_settings

log = structlog.get_logger()

HEADER_NAME = "X-Suliko-Gateway"

#: Reachable without the secret. Health must stay open so an uptime monitor or
#: a load balancer can probe it, and it reveals nothing beyond the version.
#: CORS preflight is exempt because browsers do not send custom headers on it.
EXEMPT_PATHS = frozenset({"/health"})

#: Portal file transfer is done by a browser holding a signed ticket (see
#: security/portal_tokens.py), and a browser cannot hold the gateway secret.
#: The endpoint verifies the ticket, which is bound to one user, method and
#: path and expires within minutes — a stronger credential than the shared
#: secret it stands in for here. Presenting a bogus ticket only reaches that
#: check and a 401.
PORTAL_TICKET_PARAM = "ticket"


def _is_portal_ticket_request(request: Request) -> bool:
    prefix = f"{get_settings().api_v1_prefix}/portal/"
    return (
        request.method in ("GET", "POST")
        and request.url.path.startswith(prefix)
        and PORTAL_TICKET_PARAM in request.query_params
    )


class GatewayMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        settings = get_settings()
        expected = settings.bff_shared_secret.get_secret_value()

        if not expected:
            return await call_next(request)

        if request.url.path in EXEMPT_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        if _is_portal_ticket_request(request):
            return await call_next(request)

        presented = request.headers.get(HEADER_NAME, "")

        # Constant-time: a naive == leaks the secret one byte at a time to an
        # attacker who can measure response timing.
        if not hmac.compare_digest(presented, expected):
            # Deliberately terse and deliberately 404, not 401 or 403. A
            # scanner learns nothing about what is here; a misconfigured
            # frontend is diagnosed from this log line instead.
            log.warning(
                "gateway_rejected",
                path=request.url.path,
                had_header=bool(presented),
                client=request.client.host if request.client else None,
            )
            return JSONResponse(
                status_code=404,
                content={
                    "type": "https://docs.suliko.ge/errors/not_found",
                    "title": "Not Found",
                    "status": 404,
                    "detail": "Not found.",
                    "instance": request.url.path,
                },
                media_type="application/problem+json",
            )

        return await call_next(request)
