"""Order file storage: one Suliko-owned bucket, or a folder on the server.

## The model

Suliko stores every bureau's order files itself. Nothing is asked of a bureau —
no Google Workspace, no Shared Drive, no account to connect — so file upload
works the moment an organisation exists, on every plan.

The bytes go to an object store under a key the server builds
(``domain/order_files.py``); everything a person sees — the name, the type, who
uploaded it, whether it was removed — lives in the ``order_files`` table. The
store is never listed or searched, only addressed by key, so it needs exactly
three permissions: put, get and delete an object.

## Backends

``STORAGE_BACKEND`` picks one:

- ``s3`` — any S3-compatible service: AWS S3, Cloudflare R2, Backblaze B2,
  Wasabi, MinIO. Only the endpoint differs between them.
- ``local`` — a directory on the API server. For development, and for a
  single-server deployment that would rather back up a folder than pay for a
  bucket.

Unset: order data works, file routes answer "storage is not configured".

## Why raw HTTP

Same reasoning as the old Drive client: three calls, and ``boto3`` is large,
synchronous and would need a thread per transfer. Signature Version 4 is a
handful of HMACs over ``hashlib``/``hmac`` from the standard library; it is
pinned against AWS's published example in ``tests/test_object_storage.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx
import structlog

from suliko.config import Settings, get_settings

log = structlog.get_logger()

NOT_CONFIGURED = "File storage is not configured on this server."

METADATA_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
#: Scanned documents are large, and a bureau's uplink is not always quick.
TRANSFER_TIMEOUT = httpx.Timeout(300.0, connect=10.0)

#: SHA-256 of nothing — the payload hash of every GET and DELETE.
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

#: Keys are built by the server, never taken from a request, so this is a
#: tripwire rather than input validation: path separators and dots in any
#: combination that could climb out of the local root are refused outright.
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*$")

CHUNK_SIZE = 1024 * 1024


class StorageError(Exception):
    """A storage call failed.

    ``status`` is the upstream HTTP status when there was one. The message can
    name keys and buckets, so it is logged, never returned to a caller.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_not_found(self) -> bool:
        return self.status == 404


class StorageNotConfiguredError(StorageError):
    """This server has no usable storage backend."""


class ObjectStorage(Protocol):
    """What the rest of the app needs from storage. Tests provide a fake."""

    @property
    def configured(self) -> bool: ...

    #: Shown by ``suliko check``: which backend, and where. Never a secret.
    @property
    def description(self) -> str: ...

    async def put(self, key: str, content: bytes, content_type: str) -> None: ...

    def iter_get(self, key: str) -> AsyncIterator[bytes]: ...

    async def delete(self, key: str) -> None: ...


def _check_key(key: str) -> str:
    if len(key) > 500 or not KEY_PATTERN.fullmatch(key):
        raise StorageError("refusing a malformed storage key")
    return key


# ── AWS Signature Version 4 ─────────────────────────────────────────────────


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(value: str, *, keep_slash: bool) -> str:
    """RFC 3986 encoding the way SigV4 wants it: unreserved characters kept."""
    return quote(value, safe="-_.~/" if keep_slash else "-_.~")


def sigv4_authorization(
    *,
    method: str,
    host: str,
    path: str,
    query: Mapping[str, str],
    headers: Mapping[str, str],
    payload_sha256: str,
    amz_date: str,
    access_key_id: str,
    secret_access_key: str,
    region: str,
    service: str = "s3",
) -> str:
    """The ``Authorization`` header for one request.

    ``headers`` are the headers to sign, besides ``host``. They must include
    ``x-amz-date`` and ``x-amz-content-sha256`` and must be sent exactly as
    given. ``path`` is already URI-encoded.
    """
    signed = {"host": host, **{k.lower(): v for k, v in headers.items()}}
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{' '.join(signed[name].split())}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_query = "&".join(
        f"{_uri_encode(k, keep_slash=False)}={_uri_encode(v, keep_slash=False)}"
        for k, v in sorted(query.items())
    )
    canonical_request = "\n".join(
        (method, path, canonical_query, canonical_headers, signed_headers, payload_sha256)
    )

    date = amz_date[:8]
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
    )

    key = _hmac(f"AWS4{secret_access_key}".encode(), date)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def _s3_message(response: httpx.Response) -> str:
    """The S3 error code, from the XML body most providers send."""
    match = re.search(r"<Code>([^<]{1,100})</Code>", response.text[:2000])
    return f"storage returned {response.status_code}: {match.group(1) if match else 'no code'}"


# ── S3-compatible ───────────────────────────────────────────────────────────


class S3Storage:
    def __init__(
        self,
        *,
        endpoint_url: str,
        region: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        addressing_style: str = "path",
        http: httpx.AsyncClient | None = None,
    ) -> None:
        parts = urlsplit(endpoint_url.rstrip("/"))
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise StorageNotConfiguredError("S3_ENDPOINT_URL is not an http(s) URL")
        self._scheme = parts.scheme
        self._endpoint_host = parts.netloc
        self._region = region
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._virtual = addressing_style == "virtual"
        self._http = http or httpx.AsyncClient(timeout=METADATA_TIMEOUT)

    @property
    def configured(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return f"s3 bucket {self._bucket!r} at {self._endpoint_host} ({self._region})"

    async def aclose(self) -> None:
        await self._http.aclose()

    def _target(self, key: str) -> tuple[str, str, str]:
        """(url, host, encoded path) for one object."""
        encoded_key = _uri_encode(_check_key(key), keep_slash=True)
        if self._virtual:
            host = f"{self._bucket}.{self._endpoint_host}"
            path = f"/{encoded_key}"
        else:
            host = self._endpoint_host
            path = f"/{_uri_encode(self._bucket, keep_slash=False)}/{encoded_key}"
        return f"{self._scheme}://{host}{path}", host, path

    def _headers(
        self,
        method: str,
        host: str,
        path: str,
        payload_sha256: str,
        extra: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        to_sign = {
            "x-amz-content-sha256": payload_sha256,
            "x-amz-date": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
            **(extra or {}),
        }
        authorization = sigv4_authorization(
            method=method,
            host=host,
            path=path,
            query={},
            headers=to_sign,
            payload_sha256=payload_sha256,
            amz_date=to_sign["x-amz-date"],
            access_key_id=self._access_key_id,
            secret_access_key=self._secret_access_key,
            region=self._region,
        )
        # Host is sent explicitly so it is byte-for-byte what was signed.
        return {"Host": host, "Authorization": authorization, **to_sign}

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        url, host, path = self._target(key)
        digest = hashlib.sha256(content).hexdigest()
        headers = self._headers("PUT", host, path, digest, {"content-type": content_type})
        try:
            response = await self._http.put(
                url, content=content, headers=headers, timeout=TRANSFER_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise StorageError(f"writing to storage failed: {type(exc).__name__}") from exc
        if response.status_code >= 300:
            raise StorageError(_s3_message(response), response.status_code)

    async def iter_get(self, key: str) -> AsyncIterator[bytes]:
        url, host, path = self._target(key)
        headers = self._headers("GET", host, path, EMPTY_SHA256)
        try:
            async with self._http.stream(
                "GET", url, headers=headers, timeout=TRANSFER_TIMEOUT
            ) as response:
                if response.status_code >= 300:
                    await response.aread()
                    raise StorageError(_s3_message(response), response.status_code)
                async for chunk in response.aiter_bytes():
                    yield chunk
        except httpx.HTTPError as exc:
            raise StorageError(f"reading from storage failed: {type(exc).__name__}") from exc

    async def delete(self, key: str) -> None:
        url, host, path = self._target(key)
        headers = self._headers("DELETE", host, path, EMPTY_SHA256)
        try:
            response = await self._http.delete(url, headers=headers)
        except httpx.HTTPError as exc:
            raise StorageError(f"removing from storage failed: {type(exc).__name__}") from exc
        # S3 answers 204 whether or not the object existed; a 404 from a
        # stricter clone means the same thing — it is gone.
        if response.status_code >= 300 and response.status_code != 404:
            raise StorageError(_s3_message(response), response.status_code)


# ── A directory on the server ───────────────────────────────────────────────


class LocalDiskStorage:
    """Files under one root directory, at their key's path.

    Writes go to a temporary name and are renamed into place, so a crash or a
    full disk mid-upload never leaves a truncated file under a real key.
    """

    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()

    @property
    def configured(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return f"local directory {self._root}"

    def _path(self, key: str) -> Path:
        path = (self._root / _check_key(key)).resolve()
        # Belt and braces after the key pattern.
        if self._root not in path.parents:
            raise StorageError("refusing a key outside the storage root")
        return path

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(f"{path.name}.{os.getpid()}.part")
            try:
                partial.write_bytes(content)
                os.replace(partial, path)
            finally:
                partial.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(write)
        except OSError as exc:
            raise StorageError(f"writing to local storage failed: {type(exc).__name__}") from exc

    async def iter_get(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        try:
            handle = await asyncio.to_thread(path.open, "rb")
        except FileNotFoundError as exc:
            raise StorageError("no such object", 404) from exc
        except OSError as exc:
            raise StorageError(f"reading local storage failed: {type(exc).__name__}") from exc
        try:
            while chunk := await asyncio.to_thread(handle.read, CHUNK_SIZE):
                yield chunk
        finally:
            handle.close()

    async def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as exc:
            raise StorageError(f"deleting from local storage failed: {type(exc).__name__}") from exc


# ── When nothing is configured ──────────────────────────────────────────────


class _NotConfiguredIterator:
    def __aiter__(self) -> _NotConfiguredIterator:
        return self

    async def __anext__(self) -> bytes:
        raise StorageNotConfiguredError(NOT_CONFIGURED)


class UnconfiguredStorage:
    """Stands in when no backend is configured, so callers need no ``None`` checks."""

    @property
    def configured(self) -> bool:
        return False

    @property
    def description(self) -> str:
        return "not configured"

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        raise StorageNotConfiguredError(NOT_CONFIGURED)

    def iter_get(self, key: str) -> AsyncIterator[bytes]:
        return _NotConfiguredIterator()

    async def delete(self, key: str) -> None:
        raise StorageNotConfiguredError(NOT_CONFIGURED)


def build_storage(settings: Settings) -> ObjectStorage:
    """The backend the settings describe. Never raises: a broken
    configuration is logged and treated as no configuration, so everything
    except files keeps working."""
    backend = settings.storage_backend
    try:
        if backend == "local":
            if not settings.storage_local_dir:
                raise StorageNotConfiguredError("STORAGE_LOCAL_DIR is not set")
            return LocalDiskStorage(settings.storage_local_dir)
        if backend == "s3":
            secret = (
                settings.s3_secret_access_key.get_secret_value()
                if settings.s3_secret_access_key
                else ""
            )
            missing = [
                name
                for name, value in (
                    ("S3_BUCKET", settings.s3_bucket),
                    ("S3_ACCESS_KEY_ID", settings.s3_access_key_id),
                    ("S3_SECRET_ACCESS_KEY", secret),
                )
                if not value
            ]
            if missing:
                raise StorageNotConfiguredError(f"missing {', '.join(missing)}")
            return S3Storage(
                # AWS needs no endpoint: it follows from the region.
                endpoint_url=settings.s3_endpoint_url
                or f"https://s3.{settings.s3_region}.amazonaws.com",
                region=settings.s3_region,
                bucket=str(settings.s3_bucket),
                access_key_id=str(settings.s3_access_key_id),
                secret_access_key=secret,
                addressing_style=settings.s3_addressing_style,
            )
    except StorageNotConfiguredError as exc:
        # Loud, but not fatal.
        log.error("storage_misconfigured", backend=backend, error=str(exc))
    return UnconfiguredStorage()


_storage: ObjectStorage | None = None


def get_object_storage() -> ObjectStorage:
    """FastAPI dependency. Built once per process; tests override it."""
    global _storage
    if _storage is None:
        _storage = build_storage(get_settings())
    return _storage


async def close_object_storage() -> None:
    global _storage
    if isinstance(_storage, S3Storage):
        await _storage.aclose()
    _storage = None
