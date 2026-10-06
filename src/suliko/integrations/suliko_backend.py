"""suliko.ge's backend: who a person is, and whether their password is right.

People registered on suliko.ge use Suliko Office with the same sign-in. Their
password stays THERE — Office never stores it — so signing in means asking
suliko.ge:

- `check_password` calls the public `POST /api/Auth/token`, exactly as any
  suliko.ge client would. It has no side effects (unlike `login-with-phone`,
  which overwrites the refresh token and would sign the person out of
  suliko.ge). A right password returns a token carrying the person's id.
- `get_user`, `find_user` and `iter_users` call the read-only user directory
  (`/api/office/users`), behind the shared key `SULIKO_API_KEY` sent as
  `X-Office-Key`. It says who a person is: their id, the sign-in name they
  proved at registration, and their name. Never a password or a token.

Three outcomes are kept apart on purpose. A wrong password is `REJECTED`. A
suliko.ge that is down, slow or misconfigured is `UNAVAILABLE` — never a
rejection, or an outage there would read as "wrong password" to everyone and
count against their login attempts. Only 400 and 401 are a verdict; anything
else unexpected is logged and treated as unavailable.

Nothing here logs a password, a token or a sign-in name.
"""

from __future__ import annotations

import base64
import binascii
import enum
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import structlog

from suliko.config import get_settings
from suliko.core.errors import UpstreamUnavailableError

log = structlog.get_logger()

TOKEN_PATH = "/api/Auth/token"  # noqa: S105 -- a URL path, not a secret
USERS_PATH = "/api/office/users"
KEY_HEADER = "X-Office-Key"
#: The claim suliko.ge puts the person's id in (see `AuthController.GenerateToken`).
USER_ID_CLAIM = "UserId"

PAGE_SIZE = 200


class SulikoUnavailableError(UpstreamUnavailableError):
    """suliko.ge could not be asked, or answered something unusable."""


class PasswordOutcome(enum.Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class PasswordCheck:
    outcome: PasswordOutcome
    #: suliko.ge's id for the person — set only when ACCEPTED.
    user_id: str | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is PasswordOutcome.ACCEPTED


@dataclass(frozen=True, slots=True)
class SulikoUser:
    """One person on suliko.ge, as much of them as Office needs."""

    id: str
    #: The sign-in name proven at registration: an email address, or a phone
    #: number. NOT the profile email or phone, which can be edited unverified.
    user_name: str
    first_name: str
    last_name: str
    #: "normal", "google" or "facebook".
    user_type: str
    created_at: datetime | None

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name.strip(), self.last_name.strip()) if part)

    @property
    def signs_in_with_email(self) -> bool:
        return "@" in self.user_name

    @property
    def email(self) -> str | None:
        """The sign-in address, lower-cased — or None for a phone sign-in."""
        return self.user_name.strip().lower() if self.signs_in_with_email else None

    @property
    def phone(self) -> str | None:
        """The sign-in phone number as suliko.ge holds it — None for an address."""
        return None if self.signs_in_with_email else self.user_name.strip()


class SulikoBackend(Protocol):
    """What the rest of Office may ask of suliko.ge. Tests substitute a fake."""

    @property
    def enabled(self) -> bool: ...

    async def check_password(self, login: str, password: str) -> PasswordCheck: ...

    async def get_user(self, user_id: str) -> SulikoUser | None: ...

    async def find_user(self, login: str) -> SulikoUser | None: ...

    def iter_users(self, page_size: int = PAGE_SIZE) -> AsyncIterator[SulikoUser]: ...

    async def aclose(self) -> None: ...


def _field(data: dict[str, Any], name: str) -> Any:
    """A JSON field by name, whatever the casing the server serialises with."""
    wanted = name.lower()
    for key, value in data.items():
        if key.lower() == wanted:
            return value
    return None


def parse_user(data: Any) -> SulikoUser:
    """A directory row, or SulikoUnavailableError if it is not one."""
    if not isinstance(data, dict):
        raise SulikoUnavailableError("suliko.ge sent a user Office could not read.")
    user_id = _field(data, "id")
    user_name = _field(data, "userName")
    if not isinstance(user_id, str) or not user_id or not isinstance(user_name, str):
        raise SulikoUnavailableError("suliko.ge sent a user Office could not read.")
    created: datetime | None = None
    raw_created = _field(data, "createdAt")
    if isinstance(raw_created, str):
        try:
            created = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
        except ValueError:
            created = None
    return SulikoUser(
        id=user_id,
        user_name=user_name.strip(),
        first_name=str(_field(data, "firstName") or ""),
        last_name=str(_field(data, "lastName") or ""),
        user_type=str(_field(data, "userType") or "normal").lower(),
        created_at=created,
    )


def _is_not_found(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("error") == "not_found"


def user_id_from_token(access_token: str) -> str | None:
    """The person's id from the JWT suliko.ge issued.

    The token arrived over TLS straight from suliko.ge's own endpoint in answer
    to a password that endpoint just accepted, so its payload is read without
    checking the signature — Office holds no key to check it with, and nothing
    is decided on it beyond "which person is this".
    """
    parts = access_token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(claims, dict):
        return None
    value = claims.get(USER_ID_CLAIM)
    return value if isinstance(value, str) and value else None


class HttpSulikoBackend:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._http = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout, connect=5.0)
        )

    @property
    def enabled(self) -> bool:
        return True

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── Passwords ───────────────────────────────────────────────────────────

    async def check_password(self, login: str, password: str) -> PasswordCheck:
        try:
            # Form-encoded: that is what the endpoint binds (`[FromForm]`).
            response = await self._http.post(
                TOKEN_PATH, data={"username": login, "password": password}
            )
        except httpx.HTTPError as exc:
            log.error("suliko_backend_unreachable", error=type(exc).__name__)
            return PasswordCheck(PasswordOutcome.UNAVAILABLE)

        if response.status_code in (400, 401):
            return PasswordCheck(PasswordOutcome.REJECTED)
        if response.status_code != 200:
            log.error("suliko_backend_unexpected_status", status=response.status_code)
            return PasswordCheck(PasswordOutcome.UNAVAILABLE)

        try:
            token = response.json().get("access_token")
        except (ValueError, AttributeError):
            token = None
        user_id = user_id_from_token(token) if isinstance(token, str) else None
        if user_id is None:
            log.error("suliko_backend_token_unreadable")
            return PasswordCheck(PasswordOutcome.UNAVAILABLE)
        return PasswordCheck(PasswordOutcome.ACCEPTED, user_id)

    # ── The directory ───────────────────────────────────────────────────────

    async def _get_json(
        self, path: str, params: dict[str, Any] | None = None, *, missing_ok: bool = False
    ) -> Any | None:
        """The answer; None for a person who is not there (`missing_ok`); or
        SulikoUnavailableError.

        A 404 only means "no such person" when it carries suliko.ge's own
        `{"error": "not_found"}`. The bare 404 an unconfigured suliko.ge gives
        for every directory route is a setup problem, never "nobody".
        """
        if not self._api_key:
            raise SulikoUnavailableError("Office is not set up to read suliko.ge's users.")
        try:
            response = await self._http.get(
                path, params=params, headers={KEY_HEADER: self._api_key}
            )
        except httpx.HTTPError as exc:
            log.error("suliko_backend_unreachable", error=type(exc).__name__)
            raise SulikoUnavailableError("suliko.ge cannot be reached right now.") from exc

        if response.status_code == 404 and missing_ok and _is_not_found(response):
            return None
        if response.status_code != 200:
            log.error("suliko_backend_directory_status", status=response.status_code, path=path)
            raise SulikoUnavailableError("suliko.ge cannot be reached right now.")
        try:
            return response.json()
        except ValueError as exc:
            raise SulikoUnavailableError("suliko.ge sent something Office could not read.") from exc

    async def get_user(self, user_id: str) -> SulikoUser | None:
        data = await self._get_json(f"{USERS_PATH}/{quote(user_id, safe='')}", missing_ok=True)
        return None if data is None else parse_user(data)

    async def find_user(self, login: str) -> SulikoUser | None:
        data = await self._get_json(
            f"{USERS_PATH}/lookup", params={"identifier": login}, missing_ok=True
        )
        return None if data is None else parse_user(data)

    async def iter_users(self, page_size: int = PAGE_SIZE) -> AsyncIterator[SulikoUser]:
        page, seen = 1, 0
        while True:
            data = await self._get_json(USERS_PATH, params={"page": page, "pageSize": page_size})
            if not isinstance(data, dict):
                raise SulikoUnavailableError("suliko.ge sent something Office could not read.")
            items = _field(data, "items")
            total = _field(data, "total")
            if not isinstance(items, list) or not isinstance(total, int):
                raise SulikoUnavailableError("suliko.ge sent something Office could not read.")
            for item in items:
                yield parse_user(item)
            seen += len(items)
            if not items or seen >= total:
                return
            page += 1


class UnconfiguredSulikoBackend:
    """No `SULIKO_API_URL`: Office signs people in on its own passwords alone."""

    @property
    def enabled(self) -> bool:
        return False

    async def aclose(self) -> None:
        return None

    async def check_password(self, login: str, password: str) -> PasswordCheck:
        return PasswordCheck(PasswordOutcome.REJECTED)

    async def get_user(self, user_id: str) -> SulikoUser | None:
        raise SulikoUnavailableError("suliko.ge is not connected to this Office.")

    async def find_user(self, login: str) -> SulikoUser | None:
        raise SulikoUnavailableError("suliko.ge is not connected to this Office.")

    def iter_users(self, page_size: int = PAGE_SIZE) -> AsyncIterator[SulikoUser]:
        # Raises when asked, not when first iterated, so a caller learns at once.
        raise SulikoUnavailableError("suliko.ge is not connected to this Office.")


_backend: SulikoBackend | None = None


def get_suliko_backend() -> SulikoBackend:
    """FastAPI dependency. Built once per process; tests override it."""
    global _backend
    if _backend is None:
        settings = get_settings()
        if settings.suliko_backend_enabled and settings.suliko_api_url:
            _backend = HttpSulikoBackend(
                settings.suliko_api_url,
                settings.suliko_api_key.get_secret_value(),
                settings.suliko_api_timeout_seconds,
            )
        else:
            _backend = UnconfiguredSulikoBackend()
    return _backend


async def close_suliko_backend() -> None:
    global _backend
    if _backend is not None:
        await _backend.aclose()
    _backend = None
