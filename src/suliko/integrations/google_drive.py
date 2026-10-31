"""Google Drive over raw HTTP, signed in as a service account.

Four calls are needed (find a folder, create one, upload a file, read the root),
so the official client library is not pulled in: it is large and synchronous,
and a new dependency has taken this API down once (see the deploy notes). The
service account signs a short-lived JWT (RS256, ``cryptography`` is already a
dependency), swaps it for an access token, and every Drive call carries
``supportsAllDrives`` because the order folders live in a shared drive.

The account must be a member of that shared drive (Content manager), or the
folder must be shared with it.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from suliko.core.errors import ValidationError

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER_MIME = "application/vnd.google-apps.folder"
TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class DriveError(Exception):
    """A Drive call failed. The message is safe to show staff."""


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _query_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


@dataclass(frozen=True)
class ServiceAccount:
    email: str
    private_key: str
    subject: str | None = None

    @classmethod
    def parse(cls, raw: str, *, subject: str | None = None) -> ServiceAccount:
        try:
            data = json.loads(raw)
            email, key = str(data["client_email"]), str(data["private_key"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(
                "That is not a service account key file (client_email and private_key are missing)."
            ) from exc
        return cls(email=email, private_key=key, subject=(subject or "").strip() or None)

    def assertion(self, *, now: int | None = None) -> str:
        issued = int(now if now is not None else time.time())
        claims = {
            "iss": self.email,
            "scope": SCOPE,
            "aud": TOKEN_URL,
            "iat": issued,
            "exp": issued + 3000,
        }
        if self.subject:
            claims["sub"] = self.subject
        head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        body = _b64(json.dumps(claims).encode())
        signing_input = head + b"." + body
        key = serialization.load_pem_private_key(self.private_key.encode(), password=None)
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
        return (signing_input + b"." + _b64(signature)).decode()


@dataclass(frozen=True)
class DriveFolder:
    id: str
    name: str


class GoogleDrive:
    """One service account's view of Drive. Cheap to build; the token is cached
    on the instance, so make one per batch of work."""

    def __init__(self, account: ServiceAccount, *, client: httpx.AsyncClient | None = None) -> None:
        self._account = account
        self._client = client
        self._token: str | None = None
        self._token_until = 0.0

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_until:
            return self._token
        try:
            assertion = self._account.assertion()
        except (ValueError, TypeError) as exc:
            raise DriveError("The service account's private key could not be read.") from exc
        http = await self._http()
        try:
            response = await http.post(
                TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        except httpx.HTTPError as exc:
            raise DriveError("Google could not be reached.") from exc
        if response.status_code != 200:
            raise DriveError(f"Google refused the service account ({_reason(response)}).")
        data = response.json()
        self._token = str(data["access_token"])
        self._token_until = time.time() + max(60, int(data.get("expires_in", 3600)) - 120)
        return self._token

    async def _send(
        self, method: str, url: str, *, headers: dict[str, str] | None = None, **kwargs: object
    ) -> httpx.Response:
        token = await self._access_token()
        http = await self._http()
        headers = {**(headers or {}), "Authorization": f"Bearer {token}"}
        try:
            response = await http.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise DriveError("Google Drive could not be reached.") from exc
        if response.status_code >= 400:
            raise DriveError(f"Google Drive refused the request ({_reason(response)}).")
        return response

    async def get_folder(self, folder_id: str) -> DriveFolder:
        response = await self._send(
            "GET",
            f"{API}/files/{folder_id}",
            params={"fields": "id,name,mimeType", "supportsAllDrives": "true"},
        )
        data = response.json()
        if data.get("mimeType") != FOLDER_MIME:
            raise DriveError("That Drive id is not a folder.")
        return DriveFolder(id=str(data["id"]), name=str(data["name"]))

    async def find_child_folder(self, parent_id: str, name: str) -> DriveFolder | None:
        query = (
            f"'{_query_literal(parent_id)}' in parents and name = '{_query_literal(name)}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        response = await self._send(
            "GET",
            f"{API}/files",
            params={
                "q": query,
                "fields": "files(id,name)",
                "pageSize": "5",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "corpora": "allDrives",
            },
        )
        files = response.json().get("files") or []
        return DriveFolder(id=str(files[0]["id"]), name=str(files[0]["name"])) if files else None

    async def create_folder(self, parent_id: str, name: str) -> DriveFolder:
        response = await self._send(
            "POST",
            f"{API}/files",
            params={"fields": "id,name", "supportsAllDrives": "true"},
            json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        )
        data = response.json()
        return DriveFolder(id=str(data["id"]), name=str(data["name"]))

    async def child_folder(self, parent_id: str, name: str) -> DriveFolder:
        """The folder called `name` under `parent_id`, made if it is not there."""
        return await self.find_child_folder(parent_id, name) or await self.create_folder(
            parent_id, name
        )

    async def upload_file(
        self, parent_id: str, name: str, content: bytes, content_type: str
    ) -> str:
        """Upload in one multipart request. Order files are capped well below
        the size where a resumable upload would matter. Returns the file id."""
        boundary = "suliko" + base64.b32encode(str(time.time_ns()).encode()).decode().lower()
        meta = json.dumps({"name": name, "parents": [parent_id]}).encode()
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + meta
            + f"\r\n--{boundary}\r\nContent-Type: {content_type}\r\n\r\n".encode()
            + content
            + f"\r\n--{boundary}--".encode()
        )
        response = await self._send(
            "POST",
            UPLOAD,
            params={"uploadType": "multipart", "fields": "id", "supportsAllDrives": "true"},
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            content=body,
        )
        return str(response.json()["id"])


def _reason(response: httpx.Response) -> str:
    """Google's short reason, never the body (it can echo file names)."""
    try:
        error = response.json().get("error")
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(error, dict):
        return str(error.get("status") or error.get("message") or response.status_code)[:80]
    return str(error or response.status_code)[:80]
