"""Google Drive, through one Suliko service account.

## The model

Suliko has a single Google service account. A bureau that wants its order files
in Drive adds that account's email to one of its Shared Drives as a *Content
manager*, and the suliko.ge admin records the drive against the tenant. Every
file for that tenant is then read and written inside that drive.

Shared Drives rather than a folder in someone's My Drive, because a service
account has no storage quota of its own: creating a file in a user's My Drive
fails with ``storageQuotaExceeded``. In a Shared Drive the drive owns the file,
so the question does not arise. Content manager rather than Contributor, because
removing a file (moving it to the drive's bin) needs it.

## Why raw HTTP

The Drive surface used here is a handful of calls. ``google-api-python-client``
is large, synchronous and discovery-document driven, and the service-account
token exchange is one signed JWT and one POST. Both are done with ``httpx`` and
``cryptography``, which the app already depends on.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import structlog
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from suliko.config import get_settings

log = structlog.get_logger()

DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 — a URL, not a secret
SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,mimeType,size,createdTime,parents,appProperties,trashed"

METADATA_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
#: Scanned documents are large and Google is not always quick with them.
TRANSFER_TIMEOUT = httpx.Timeout(300.0, connect=10.0)

NOT_CONFIGURED = "Google Drive is not configured on this server."


class DriveError(Exception):
    """A Drive call failed.

    ``status`` is Google's HTTP status when there was a response. The message is
    Google's and can name file ids, so it is logged, never returned to a caller.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_not_found(self) -> bool:
        return self.status == 404


class DriveNotConfiguredError(DriveError):
    """This server has no usable service-account key, so there is no Drive."""


@dataclass(frozen=True, slots=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size_bytes: int | None
    created_at: datetime | None
    parents: tuple[str, ...] = ()
    app_properties: Mapping[str, str] = field(default_factory=dict)
    trashed: bool = False

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME_TYPE

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> DriveFile:
        created = data.get("createdTime")
        size = data.get("size")
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or ""),
            mime_type=str(data.get("mimeType") or "application/octet-stream"),
            # Google returns size as a string, and omits it for Google Docs.
            size_bytes=int(size) if size is not None else None,
            created_at=datetime.fromisoformat(str(created)) if created else None,
            parents=tuple(str(p) for p in data.get("parents") or ()),
            app_properties={str(k): str(v) for k, v in (data.get("appProperties") or {}).items()},
            trashed=bool(data.get("trashed", False)),
        )


class DriveClient(Protocol):
    """What the rest of the app needs from Drive. Tests provide a fake."""

    @property
    def service_account_email(self) -> str | None: ...

    async def get_shared_drive_name(self, drive_id: str) -> str: ...

    async def find_folder(
        self, *, drive_id: str, parent_id: str, name: str
    ) -> DriveFile | None: ...

    async def create_folder(self, *, parent_id: str, name: str) -> DriveFile: ...

    async def list_children(self, *, drive_id: str, folder_id: str) -> list[DriveFile]: ...

    async def get_file(self, file_id: str) -> DriveFile: ...

    async def upload_file(
        self,
        *,
        parent_id: str,
        name: str,
        content: bytes,
        content_type: str,
        app_properties: Mapping[str, str],
    ) -> DriveFile: ...

    def iter_download(self, file_id: str) -> AsyncIterator[bytes]: ...

    async def trash_file(self, file_id: str) -> None: ...


# ── Service-account authentication ──────────────────────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class ServiceAccountKey:
    client_email: str
    private_key: rsa.RSAPrivateKey
    token_uri: str

    @classmethod
    def load(cls, path: str) -> ServiceAccountKey:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if data.get("type") != "service_account":
                raise DriveNotConfiguredError("the key file is not a service-account key")
            key = serialization.load_pem_private_key(
                str(data["private_key"]).encode("utf-8"), password=None
            )
            email = str(data["client_email"])
        except DriveNotConfiguredError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # The exception type only: the message of a PEM parse error can
            # quote key material.
            raise DriveNotConfiguredError(
                f"cannot use the service-account key: {type(exc).__name__}"
            ) from exc

        if not isinstance(key, rsa.RSAPrivateKey):
            raise DriveNotConfiguredError("the service-account key is not an RSA key")
        return cls(
            client_email=email,
            private_key=key,
            token_uri=str(data.get("token_uri") or TOKEN_URI),
        )

    def signed_assertion(self, now: int) -> str:
        """The JWT exchanged for an access token (RFC 7523)."""
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": self.client_email,
            "scope": SCOPE,
            "aud": self.token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        signing_input = ".".join(
            _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
        )
        signature = self.private_key.sign(
            signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
        return f"{signing_input}.{_b64url(signature)}"


def _google_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        message = body.get("error", {}).get("message") if isinstance(body, dict) else None
    except ValueError:
        message = None
    return f"Drive returned {response.status_code}: {message or response.text[:200]}"


def _quote(value: str) -> str:
    """Escape a value for a Drive search query (`q`)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ── The real client ─────────────────────────────────────────────────────────


class GoogleDriveClient:
    def __init__(self, key: ServiceAccountKey, http: httpx.AsyncClient | None = None) -> None:
        self._key = key
        self._http = http or httpx.AsyncClient(timeout=METADATA_TIMEOUT)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def service_account_email(self) -> str | None:
        return self._key.client_email

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        # Refreshed a minute early so a token never expires mid-request.
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at - 60:
                return self._token
            try:
                response = await self._http.post(
                    self._key.token_uri,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": self._key.signed_assertion(int(time.time())),
                    },
                )
            except httpx.HTTPError as exc:
                raise DriveError(f"token exchange failed: {type(exc).__name__}") from exc
            if response.status_code != 200:
                raise DriveError(_google_message(response), response.status_code)
            body = response.json()
            self._token = str(body["access_token"])
            self._token_expires_at = time.monotonic() + float(body.get("expires_in", 3600))
            return self._token

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: httpx.Timeout = METADATA_TIMEOUT,
    ) -> httpx.Response:
        token = await self._access_token()
        try:
            response = await self._http.request(
                method,
                url,
                params=params,
                json=json_body,
                content=content,
                headers={"Authorization": f"Bearer {token}", **(headers or {})},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise DriveError(f"{method} to Drive failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise DriveError(_google_message(response), response.status_code)
        return response

    async def get_shared_drive_name(self, drive_id: str) -> str:
        response = await self._request(
            "GET", f"{DRIVE_API}/drives/{drive_id}", params={"fields": "id,name"}
        )
        return str(response.json().get("name") or drive_id)

    async def _search(
        self, drive_id: str, query: str, *, page_size: int, max_pages: int
    ) -> list[DriveFile]:
        params: dict[str, str] = {
            "q": query,
            "corpora": "drive",
            "driveId": drive_id,
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "pageSize": str(page_size),
            "orderBy": "createdTime desc",
        }
        files: list[DriveFile] = []
        for _ in range(max_pages):
            body = (await self._request("GET", f"{DRIVE_API}/files", params=params)).json()
            files.extend(DriveFile.from_api(item) for item in body.get("files", []))
            next_page = body.get("nextPageToken")
            if not next_page:
                break
            params["pageToken"] = str(next_page)
        return files

    async def find_folder(self, *, drive_id: str, parent_id: str, name: str) -> DriveFile | None:
        query = (
            f"'{_quote(parent_id)}' in parents and name = '{_quote(name)}' "
            f"and mimeType = '{FOLDER_MIME_TYPE}' and trashed = false"
        )
        matches = await self._search(drive_id, query, page_size=10, max_pages=1)
        return matches[0] if matches else None

    async def create_folder(self, *, parent_id: str, name: str) -> DriveFile:
        response = await self._request(
            "POST",
            f"{DRIVE_API}/files",
            params={"supportsAllDrives": "true", "fields": FILE_FIELDS},
            json_body={"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
        )
        return DriveFile.from_api(response.json())

    async def list_children(self, *, drive_id: str, folder_id: str) -> list[DriveFile]:
        query = f"'{_quote(folder_id)}' in parents and trashed = false"
        return await self._search(drive_id, query, page_size=200, max_pages=5)

    async def get_file(self, file_id: str) -> DriveFile:
        response = await self._request(
            "GET",
            f"{DRIVE_API}/files/{file_id}",
            params={"supportsAllDrives": "true", "fields": FILE_FIELDS},
        )
        return DriveFile.from_api(response.json())

    async def upload_file(
        self,
        *,
        parent_id: str,
        name: str,
        content: bytes,
        content_type: str,
        app_properties: Mapping[str, str],
    ) -> DriveFile:
        # Resumable rather than multipart: Google documents multipart uploads
        # for files of 5 MB or less, and a scanned contract is often larger.
        session = await self._request(
            "POST",
            f"{UPLOAD_API}/files",
            params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": FILE_FIELDS},
            json_body={"name": name, "parents": [parent_id], "appProperties": dict(app_properties)},
            headers={
                "X-Upload-Content-Type": content_type,
                "X-Upload-Content-Length": str(len(content)),
            },
        )
        upload_url = session.headers.get("location")
        if not upload_url:
            raise DriveError("Drive started an upload without returning its session URL")
        response = await self._request(
            "PUT",
            upload_url,
            content=content,
            headers={"Content-Type": content_type},
            timeout=TRANSFER_TIMEOUT,
        )
        return DriveFile.from_api(response.json())

    async def iter_download(self, file_id: str) -> AsyncIterator[bytes]:
        token = await self._access_token()
        async with self._http.stream(
            "GET",
            f"{DRIVE_API}/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=TRANSFER_TIMEOUT,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise DriveError(_google_message(response), response.status_code)
            async for chunk in response.aiter_bytes():
                yield chunk

    async def trash_file(self, file_id: str) -> None:
        # The bin, not a permanent delete: recoverable in Drive for 30 days,
        # which is what a mis-click on someone's translation deserves.
        await self._request(
            "PATCH",
            f"{DRIVE_API}/files/{file_id}",
            params={"supportsAllDrives": "true", "fields": "id"},
            json_body={"trashed": True},
        )


# ── When there is no key ────────────────────────────────────────────────────


class _NotConfiguredIterator:
    def __aiter__(self) -> _NotConfiguredIterator:
        return self

    async def __anext__(self) -> bytes:
        raise DriveNotConfiguredError(NOT_CONFIGURED)


class UnconfiguredDriveClient:
    """Stands in when no key is configured, so callers need no ``None`` checks.

    Every call raises ``DriveNotConfiguredError``; order data keeps working and
    file lists report that storage is unavailable.
    """

    @property
    def service_account_email(self) -> str | None:
        return None

    async def get_shared_drive_name(self, drive_id: str) -> str:
        raise DriveNotConfiguredError(NOT_CONFIGURED)

    async def find_folder(self, *, drive_id: str, parent_id: str, name: str) -> DriveFile | None:
        raise DriveNotConfiguredError(NOT_CONFIGURED)

    async def create_folder(self, *, parent_id: str, name: str) -> DriveFile:
        raise DriveNotConfiguredError(NOT_CONFIGURED)

    async def list_children(self, *, drive_id: str, folder_id: str) -> list[DriveFile]:
        raise DriveNotConfiguredError(NOT_CONFIGURED)

    async def get_file(self, file_id: str) -> DriveFile:
        raise DriveNotConfiguredError(NOT_CONFIGURED)

    async def upload_file(
        self,
        *,
        parent_id: str,
        name: str,
        content: bytes,
        content_type: str,
        app_properties: Mapping[str, str],
    ) -> DriveFile:
        raise DriveNotConfiguredError(NOT_CONFIGURED)

    def iter_download(self, file_id: str) -> AsyncIterator[bytes]:
        return _NotConfiguredIterator()

    async def trash_file(self, file_id: str) -> None:
        raise DriveNotConfiguredError(NOT_CONFIGURED)


_client: DriveClient | None = None


def get_drive_client() -> DriveClient:
    """FastAPI dependency. Built once per process; tests override it."""
    global _client
    if _client is None:
        path = get_settings().google_service_account_file
        if not path:
            _client = UnconfiguredDriveClient()
        else:
            try:
                _client = GoogleDriveClient(ServiceAccountKey.load(path))
            except DriveNotConfiguredError as exc:
                # Loud, but not fatal: everything except files keeps working.
                log.error("drive_key_unusable", error=str(exc))
                _client = UnconfiguredDriveClient()
    return _client


async def close_drive_client() -> None:
    global _client
    if isinstance(_client, GoogleDriveClient):
        await _client.aclose()
    _client = None
