"""The client for suliko.ge's translator.

No network: `httpx.MockTransport` plays suliko.ge. What matters here is that
each answer suliko.ge can give becomes the right thing on this side: a refusal
the person can act on (not enough pages, a file it cannot read) stays apart
from suliko.ge simply not being there, and the key goes with every call that
names a person.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from suliko.integrations.suliko_backend import KEY_HEADER, SulikoUnavailableError
from suliko.integrations.suliko_translate import (
    FORMAT_PLAIN_HTML,
    FORMAT_RICH_HTML,
    HttpSulikoTranslator,
    InsufficientBalanceError,
    PreparedFile,
    SulikoAccountMissingError,
    SulikoLanguage,
    UnconfiguredSulikoTranslator,
    UnsupportedFileError,
)

KEY = "office-key-of-at-least-24-chars"
PREPARED = PreparedFile("files/abc", "application/pdf", 3, Decimal("10"))


def _client(handler: Any, key: str = KEY) -> tuple[HttpSulikoTranslator, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result = handler(request)
        return result if isinstance(result, httpx.Response) else httpx.Response(*result)

    http = httpx.AsyncClient(
        base_url="https://content.example.test", transport=httpx.MockTransport(record)
    )
    return HttpSulikoTranslator("https://content.example.test", key, 5.0, client=http), seen


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


async def _start(client: HttpSulikoTranslator, *, rich: bool = False) -> str:
    return await client.start(
        "suliko-nino",
        PREPARED,
        file_name="passport.pdf",
        target_language_id=2,
        source_language_id=1,
        output_language_id=1,
        rich=rich,
    )


# ── Preparing ───────────────────────────────────────────────────────────────


async def test_prepare_hands_the_file_over_for_the_named_person() -> None:
    client, seen = _client(
        lambda _: _json(
            200,
            # ASP.NET may serialise either casing; both are read.
            {
                "FileUri": "files/abc",
                "mimeType": "application/pdf",
                "PageCount": 3,
                "balance": 10.5,
            },
        )
    )
    prepared = await client.prepare(
        "suliko-nino", file_name="passport.pdf", content_type="application/pdf", content=b"%PDF"
    )

    assert prepared == PreparedFile("files/abc", "application/pdf", 3, Decimal("10.5"))
    [request] = seen
    assert request.url.path == "/api/office/translate/prepare"
    assert request.headers[KEY_HEADER] == KEY
    body = request.read()
    assert b'name="UserId"' in body and b"suliko-nino" in body
    assert b'filename="passport.pdf"' in body and b"%PDF" in body


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (404, {"error": "user_not_found"}, SulikoAccountMissingError),
        (400, {"error": "unsupported_file", "message": "x"}, UnsupportedFileError),
        (413, {"error": "file_too_large"}, UnsupportedFileError),
        (502, {"error": "prepare_failed"}, SulikoUnavailableError),
        # The bare 404 of a suliko.ge with no Office key set is not "no such person".
        (404, None, SulikoUnavailableError),
        (200, {"fileUri": "files/abc"}, SulikoUnavailableError),
    ],
)
async def test_prepare_tells_refusals_from_outages(
    status: int, body: Any, error: type[Exception]
) -> None:
    client, _ = _client(lambda _: _json(status, body) if body else httpx.Response(status))
    with pytest.raises(error):
        await client.prepare("u", file_name="a.pdf", content_type="application/pdf", content=b"x")


async def test_without_a_key_nothing_is_sent() -> None:
    client, seen = _client(lambda _: _json(200, {}), key="")
    with pytest.raises(SulikoUnavailableError):
        await client.prepare("u", file_name="a.pdf", content_type="application/pdf", content=b"x")
    assert seen == []


# ── Starting ────────────────────────────────────────────────────────────────


async def test_start_names_the_person_the_file_and_the_languages() -> None:
    client, seen = _client(lambda _: _json(202, {"JobId": "job-1", "ChatId": "c"}))

    assert await _start(client) == "job-1"
    assert await _start(client, rich=True) == "job-1"

    plain, rich = (json.loads(request.content) for request in seen)
    assert plain == {
        "userId": "suliko-nino",
        "fileUri": "files/abc",
        "mimeType": "application/pdf",
        "fileName": "passport.pdf",
        "targetLanguageId": 2,
        "sourceLanguageId": 1,
        "outputLanguageId": 1,
        "outputFormat": FORMAT_PLAIN_HTML,
        "pageCount": 3,
    }
    assert rich["outputFormat"] == FORMAT_RICH_HTML
    assert all(request.headers[KEY_HEADER] == KEY for request in seen)


async def test_a_balance_that_does_not_cover_it_carries_the_numbers() -> None:
    client, _ = _client(lambda _: _json(402, {"error": "insufficient_balance", "balance": 2}))
    with pytest.raises(InsufficientBalanceError) as refused:
        await _start(client)
    assert refused.value.extra == {"pages": 3, "balance": "2"}


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (404, {"error": "user_not_found"}, SulikoAccountMissingError),
        (400, "MIME type 'x' is not supported", UnsupportedFileError),
        (500, "boom", SulikoUnavailableError),
        (202, {"message": "no job id"}, SulikoUnavailableError),
    ],
)
async def test_start_tells_refusals_from_outages(
    status: int, body: Any, error: type[Exception]
) -> None:
    client, _ = _client(
        lambda _: (
            _json(status, body) if isinstance(body, dict) else httpx.Response(status, text=body)
        )
    )
    with pytest.raises(error):
        await _start(client)


# ── Following ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("answer", "state"),
    [
        ("Processing", "processing"),
        ("Pending", "processing"),
        ("Completed", "completed"),
        ("Failed", "failed"),
    ],
)
async def test_status_is_one_of_three_states(answer: str, state: str) -> None:
    client, seen = _client(
        lambda _: _json(200, {"JobId": "job 1", "Status": answer, "Progress": 70, "Message": "m"})
    )
    status = await client.status("job 1")
    assert (status.state, status.progress, status.message) == (state, 70, "m")
    assert seen[0].url.raw_path == b"/api/office/translate/job%201/status"


async def test_a_job_suliko_has_forgotten_counts_as_failed() -> None:
    client, _ = _client(lambda _: _json(404, {"JobId": "x", "Message": "Job not found"}))
    assert (await client.status("x")).state == "failed"


async def test_result_is_the_file_and_its_name() -> None:
    client, _ = _client(
        lambda _: httpx.Response(
            200,
            content=b"<p>Hello</p>",
            headers={
                "content-type": "text/html",
                "content-disposition": (
                    "attachment; filename=translated.html; filename*=UTF-8''%E1%83%90.html"
                ),
            },
        )
    )
    result = await client.result("job-1")
    assert (result.content, result.content_type, result.file_name) == (
        b"<p>Hello</p>",
        "text/html",
        "ა.html",
    )


async def test_a_result_that_is_not_ready_is_not_a_file() -> None:
    client, _ = _client(lambda _: _json(400, {"Status": "Processing"}))
    with pytest.raises(SulikoUnavailableError):
        await client.result("job-1")


# ── Languages ───────────────────────────────────────────────────────────────


async def test_languages_are_read_once_and_remembered() -> None:
    client, seen = _client(
        lambda _: _json(
            200, [{"id": 1, "name": "Georgian", "nameGeo": "ქართული"}, {"Id": 2, "Name": "English"}]
        )
    )
    first = await client.languages()
    assert first == [SulikoLanguage(1, "Georgian", "ქართული"), SulikoLanguage(2, "English", "")]
    assert await client.languages() == first
    assert len(seen) == 1
    # The public list: no key is needed, so none is sent.
    assert seen[0].url.path == "/api/Language/public"
    assert KEY_HEADER not in seen[0].headers


async def test_unconfigured_says_so_for_every_call() -> None:
    off = UnconfiguredSulikoTranslator()
    assert off.enabled is False
    with pytest.raises(SulikoUnavailableError):
        await off.languages()
    with pytest.raises(SulikoUnavailableError):
        await off.status("x")
