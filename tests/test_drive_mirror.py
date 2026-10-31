"""Google Drive mirror: the Drive client (against a fake Google) and the folder
logic. The database glue (`sync_order`) is exercised on a real run, not here."""

from __future__ import annotations

import base64
import json
from datetime import date

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from suliko.core.errors import ValidationError
from suliko.domain.drive_mirror import (
    SOURCE_FOLDER,
    TRANSLATED_FOLDER,
    ensure_folders,
    order_folder_name,
)
from suliko.integrations.google_drive import (
    FOLDER_MIME,
    DriveError,
    DriveFolder,
    GoogleDrive,
    ServiceAccount,
)


def key_file(*, subject: str | None = None) -> ServiceAccount:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    raw = json.dumps({"client_email": "bot@proj.iam.gserviceaccount.com", "private_key": pem})
    return ServiceAccount.parse(raw, subject=subject)


class FakeGoogle:
    def __init__(self, *, token_status: int = 200, existing: dict[str, str] | None = None) -> None:
        self.token_status = token_status
        self.existing = existing or {}
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.uploads: list[bytes] = []
        self.created: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.calls.append((request.method, url.path, dict(url.params)))
        if url.host == "oauth2.googleapis.com":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer tok"
        assert url.params["supportsAllDrives"] == "true"
        if url.path.startswith("/upload"):
            self.uploads.append(request.read())
            return httpx.Response(200, json={"id": "file-1"})
        if request.method == "GET" and url.path == "/drive/v3/files":
            name = url.params["q"].split("name = '")[1].split("' and")[0]
            hit = self.existing.get(name)
            return httpx.Response(200, json={"files": [{"id": hit, "name": name}] if hit else []})
        if request.method == "GET":
            return httpx.Response(
                200, json={"id": "root", "name": "Translations", "mimeType": FOLDER_MIME}
            )
        body = json.loads(request.read())
        self.created.append(body)
        return httpx.Response(200, json={"id": f"new-{len(self.created)}", "name": body["name"]})


def drive(fake: FakeGoogle, account: ServiceAccount | None = None) -> GoogleDrive:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return GoogleDrive(account or key_file(), client=client)


def decode(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def test_assertion_is_a_signed_jwt_for_the_drive_scope() -> None:
    token = key_file(subject="office@example.ge").assertion(now=1000)
    head, body, signature = token.split(".")
    assert decode(head) == {"alg": "RS256", "typ": "JWT"}
    claims = decode(body)
    assert claims["iss"] == "bot@proj.iam.gserviceaccount.com"
    assert claims["scope"].endswith("/auth/drive")
    assert claims["sub"] == "office@example.ge"
    assert claims["exp"] - claims["iat"] <= 3600
    assert signature


def test_a_file_that_is_not_a_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        ServiceAccount.parse('{"hello": 1}')
    with pytest.raises(ValidationError):
        ServiceAccount.parse("not json")


async def test_finds_an_existing_folder_and_creates_a_missing_one() -> None:
    fake = FakeGoogle(existing={"A": "id-a"})
    client = drive(fake)
    assert (await client.child_folder("root", "A")).id == "id-a"
    assert (await client.child_folder("root", "B")).id == "new-1"
    assert fake.created == [{"name": "B", "mimeType": FOLDER_MIME, "parents": ["root"]}]
    # one token for all of it
    assert sum(1 for c in fake.calls if c[1] == "/token") == 1


async def test_quotes_in_a_name_cannot_break_out_of_the_query() -> None:
    fake = FakeGoogle()
    await drive(fake).find_child_folder("root", "O'Neil")
    query = next(c[2]["q"] for c in fake.calls if "q" in c[2])
    assert "name = 'O\\'Neil'" in query


async def test_upload_sends_metadata_and_bytes_in_one_request() -> None:
    fake = FakeGoogle()
    file_id = await drive(fake).upload_file("src", "ASUS.pdf", b"%PDF-bytes", "application/pdf")
    assert file_id == "file-1"
    body = fake.uploads[0]
    assert b'"parents": ["src"]' in body and b'"name": "ASUS.pdf"' in body
    assert b"%PDF-bytes" in body


async def test_google_refusing_the_key_is_a_readable_error() -> None:
    with pytest.raises(DriveError, match="refused the service account"):
        await drive(FakeGoogle(token_status=400)).get_folder("root")


def test_folder_name_follows_the_shared_drive_convention() -> None:
    name = order_folder_name(order_date=date(2026, 10, 30), number=1434, client_name="Acme / Ltd")
    assert name == "30.10.2026 - #001434 - Acme Ltd"
    assert order_folder_name(order_date=date(2026, 1, 2), number=7, client_name="") == (
        "02.01.2026 - #000007 - Client"
    )


class FakeFolders:
    def __init__(self) -> None:
        self.made: list[tuple[str, str]] = []

    async def child_folder(self, parent_id: str, name: str) -> DriveFolder:
        self.made.append((parent_id, name))
        return DriveFolder(id=f"{parent_id}/{name}", name=name)


async def test_new_order_gets_its_folder_and_both_subfolders() -> None:
    drive_ = FakeFolders()
    ids = await ensure_folders(drive_, root_id="root", order_folder_id=None, name="N")
    assert ids == ("root/N", f"root/N/{SOURCE_FOLDER}", f"root/N/{TRANSLATED_FOLDER}")
    assert [m[1] for m in drive_.made] == ["N", SOURCE_FOLDER, TRANSLATED_FOLDER]


async def test_an_imported_order_keeps_its_folder() -> None:
    drive_ = FakeFolders()
    ids = await ensure_folders(drive_, root_id="root", order_folder_id="legacy", name="N")
    assert ids[0] == "legacy"
    assert ("root", "N") not in drive_.made
