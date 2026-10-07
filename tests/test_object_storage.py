"""The storage backends, with no network and no bucket.

The S3 signer is pinned against the worked example in AWS's own Signature
Version 4 documentation ("Example: GET Object"), so a regression in the
canonicalisation shows up here rather than as a 403 from a real bucket.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from suliko.config import Settings
from suliko.integrations.object_storage import (
    EMPTY_SHA256,
    LocalDiskStorage,
    S3Storage,
    StorageError,
    StorageNotConfiguredError,
    UnconfiguredStorage,
    build_storage,
    sigv4_authorization,
)

DATABASE_URL = "postgresql+asyncpg://u:p@localhost/db"


# ── Signature Version 4 ─────────────────────────────────────────────────────


def test_sigv4_matches_the_aws_documentation_example() -> None:
    authorization = sigv4_authorization(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/test.txt",
        query={},
        headers={
            "range": "bytes=0-9",
            "x-amz-content-sha256": EMPTY_SHA256,
            "x-amz-date": "20130524T000000Z",
        },
        payload_sha256=EMPTY_SHA256,
        amz_date="20130524T000000Z",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1",
    )
    assert authorization == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


async def test_s3_requests_are_signed_and_addressed_path_style() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, content=b"hello")
        return httpx.Response(200 if request.method == "PUT" else 204)

    storage = S3Storage(
        endpoint_url="https://abc.r2.cloudflarestorage.com",
        region="auto",
        bucket="suliko-files",
        access_key_id="AK",
        secret_access_key="SK",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    key = "tenants/1/orders/2/documents/3/AbC_-x"
    await storage.put(key, b"hello", "application/pdf")
    assert b"".join([c async for c in storage.iter_get(key)]) == b"hello"
    await storage.delete(key)

    assert [r.method for r in seen] == ["PUT", "GET", "DELETE"]
    for request in seen:
        assert str(request.url) == f"https://abc.r2.cloudflarestorage.com/suliko-files/{key}"
        assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/")
        assert "/auto/s3/aws4_request" in request.headers["authorization"]
    assert seen[0].headers["x-amz-content-sha256"] != EMPTY_SHA256
    assert "content-type" in seen[0].headers["authorization"]


async def test_s3_errors_carry_the_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<Error><Code>NoSuchKey</Code></Error>")

    storage = S3Storage(
        endpoint_url="https://s3.eu-north-1.amazonaws.com",
        region="eu-north-1",
        bucket="b",
        access_key_id="AK",
        secret_access_key="SK",
        addressing_style="virtual",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(StorageError) as caught:
        async for _ in storage.iter_get("missing"):
            pass
    assert caught.value.is_not_found
    assert "NoSuchKey" in str(caught.value)

    # A delete of something already gone is not a failure.
    await storage.delete("missing")


# ── A directory on the server ───────────────────────────────────────────────


async def test_local_round_trip(tmp_path: Path) -> None:
    storage = LocalDiskStorage(str(tmp_path))
    key = "tenants/1/orders/2/documents/3/fileid"
    await storage.put(key, b"x" * 3_000_000, "application/pdf")

    assert (tmp_path / "tenants/1/orders/2/documents/3/fileid").stat().st_size == 3_000_000
    assert not list(tmp_path.rglob("*.part"))
    body = b"".join([chunk async for chunk in storage.iter_get(key)])
    assert body == b"x" * 3_000_000

    await storage.delete(key)
    await storage.delete(key)  # twice is fine
    with pytest.raises(StorageError) as caught:
        async for _ in storage.iter_get(key):
            pass
    assert caught.value.is_not_found


@pytest.mark.parametrize(
    "key", ["../escape", "a/../../b", "/absolute", "C:/windows", "a//b", "a/b/", "", "a\\b"]
)
async def test_keys_that_could_escape_the_root_are_refused(tmp_path: Path, key: str) -> None:
    storage = LocalDiskStorage(str(tmp_path / "root"))
    with pytest.raises(StorageError):
        await storage.put(key, b"x", "text/plain")


# ── Choosing a backend ──────────────────────────────────────────────────────


def _settings(**values: object) -> Settings:
    return Settings(database_url=DATABASE_URL, **values)  # type: ignore[arg-type]


def test_nothing_configured_is_unconfigured() -> None:
    assert isinstance(build_storage(_settings()), UnconfiguredStorage)


def test_local_needs_a_directory(tmp_path: Path) -> None:
    assert isinstance(build_storage(_settings(storage_backend="local")), UnconfiguredStorage)
    storage = build_storage(_settings(storage_backend="local", storage_local_dir=str(tmp_path)))
    assert isinstance(storage, LocalDiskStorage)


def test_s3_needs_its_credentials() -> None:
    incomplete = _settings(storage_backend="s3", s3_bucket="b", s3_access_key_id="AK")
    assert isinstance(build_storage(incomplete), UnconfiguredStorage)

    complete = _settings(
        storage_backend="s3",
        s3_bucket="b",
        s3_access_key_id="AK",
        s3_secret_access_key=SecretStr("SK"),
        s3_region="eu-north-1",
    )
    storage = build_storage(complete)
    assert isinstance(storage, S3Storage)
    # No endpoint: AWS's, from the region.
    assert "s3.eu-north-1.amazonaws.com" in storage.description


async def test_unconfigured_storage_refuses_everything() -> None:
    storage = UnconfiguredStorage()
    with pytest.raises(StorageNotConfiguredError):
        await storage.put("k", b"", "text/plain")
    with pytest.raises(StorageNotConfiguredError):
        async for _ in storage.iter_get("k"):
            pass


def test_the_drive_era_size_limit_still_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRIVE_FILE_MAX_BYTES", "1234")
    assert _settings().order_file_max_bytes == 1234
