"""The client for suliko.ge's backend: what it asks, and what it makes of the answers.

No network: `httpx.MockTransport` plays suliko.ge. What matters here is the
difference between three outcomes that a careless client would collapse —
a wrong password, a person who is not there, and suliko.ge being unavailable.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from suliko.integrations.suliko_backend import (
    KEY_HEADER,
    HttpSulikoBackend,
    PasswordOutcome,
    SulikoUnavailableError,
    UnconfiguredSulikoBackend,
    parse_user,
    user_id_from_token,
)

KEY = "k" * 32


def _jwt(claims: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(claims)}.signature"


def _backend(handler: Any, key: str = KEY) -> tuple[HttpSulikoBackend, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result = handler(request)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[no-any-return]

    client = httpx.AsyncClient(
        base_url="https://content.example.test", transport=httpx.MockTransport(record)
    )
    return HttpSulikoBackend("https://content.example.test", key, 5.0, client=client), seen


def _person(**extra: Any) -> dict[str, Any]:
    return {
        "id": "guid-1",
        "userName": "nino@suliko.ge",
        "firstName": "Nino",
        "lastName": "Beridze",
        "userType": "normal",
        "createdAt": "2026-01-02T03:04:05Z",
        **extra,
    }


# ── Passwords ───────────────────────────────────────────────────────────────


async def test_a_right_password_is_accepted_with_the_persons_id() -> None:
    token = _jwt({"UserId": "guid-1", "sub": "ignored"})
    backend, seen = _backend(lambda r: httpx.Response(200, json={"access_token": token}))

    check = await backend.check_password("nino@suliko.ge", "pw")

    assert check.accepted and check.user_id == "guid-1"
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/api/Auth/token"
    # Form-encoded — that is what the endpoint binds — and no directory key:
    # a password check is the public endpoint, used as any client would.
    assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert request.read() == b"username=nino%40suliko.ge&password=pw"
    assert KEY_HEADER.lower() not in request.headers


@pytest.mark.parametrize("status", [400, 401])
async def test_a_wrong_password_is_rejected(status: int) -> None:
    backend, _ = _backend(lambda r: httpx.Response(status, json={"error": "invalid credentials"}))

    check = await backend.check_password("nino@suliko.ge", "nope")

    assert check.outcome is PasswordOutcome.REJECTED and check.user_id is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(502),
        httpx.Response(503),
        httpx.Response(404),  # a wrong base URL is a setup fault, not "wrong password"
        httpx.Response(403),
        httpx.Response(429),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"access_token": "not-a-jwt"}),
        httpx.Response(200, json={"access_token": _jwt({"sub": "no-user-id-claim"})}),
        httpx.Response(200, json={}),
    ],
)
async def test_anything_unexpected_is_unavailable_never_a_rejection(
    response: httpx.Response,
) -> None:
    """An outage on suliko.ge must not read as "wrong password" to everyone."""
    backend, _ = _backend(lambda r: response)

    assert (await backend.check_password("a@b.ge", "pw")).outcome is PasswordOutcome.UNAVAILABLE


@pytest.mark.parametrize("error", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
async def test_an_unreachable_suliko_is_unavailable(error: Exception) -> None:
    backend, _ = _backend(lambda r: error)

    assert (await backend.check_password("a@b.ge", "pw")).outcome is PasswordOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (_jwt({"UserId": "abc"}), "abc"),
        (_jwt({"UserId": ""}), None),
        (_jwt({"UserId": 7}), None),
        (_jwt({}), None),
        ("a.b", None),
        ("a.!!!.c", None),
        (f"a.{base64.urlsafe_b64encode(b'[1]').decode()}.c", None),
    ],
)
def test_the_persons_id_comes_from_the_user_id_claim(token: str, expected: str | None) -> None:
    assert user_id_from_token(token) == expected


# ── The directory ───────────────────────────────────────────────────────────


async def test_the_directory_is_asked_with_the_key() -> None:
    backend, seen = _backend(lambda r: httpx.Response(200, json=_person()))

    person = await backend.get_user("guid-1")

    assert person is not None and person.id == "guid-1"
    assert seen[0].url.path == "/api/office/users/guid-1"
    assert seen[0].headers[KEY_HEADER] == KEY


async def test_a_person_who_is_not_there_is_none() -> None:
    backend, _ = _backend(lambda r: httpx.Response(404, json={"error": "not_found"}))

    assert await backend.get_user("missing") is None
    assert await backend.find_user("nobody@suliko.ge") is None


async def test_a_bare_404_is_a_setup_problem_not_nobody() -> None:
    """An unconfigured suliko.ge answers 404 to every directory route. That must
    never be read as "no such person", or every invitee would look unregistered."""
    backend, _ = _backend(lambda r: httpx.Response(404))

    with pytest.raises(SulikoUnavailableError):
        await backend.find_user("nino@suliko.ge")
    with pytest.raises(SulikoUnavailableError):
        await backend.get_user("guid-1")


@pytest.mark.parametrize("status", [401, 500, 503])
async def test_a_failing_directory_is_unavailable(status: int) -> None:
    backend, _ = _backend(lambda r: httpx.Response(status))

    with pytest.raises(SulikoUnavailableError):
        await backend.find_user("nino@suliko.ge")


async def test_an_unreachable_directory_is_unavailable() -> None:
    backend, _ = _backend(lambda r: httpx.ConnectError("down"))

    with pytest.raises(SulikoUnavailableError):
        await backend.get_user("guid-1")


async def test_without_a_key_the_directory_is_never_called() -> None:
    backend, seen = _backend(lambda r: httpx.Response(200, json=_person()), key="")

    with pytest.raises(SulikoUnavailableError):
        await backend.find_user("nino@suliko.ge")
    assert seen == []


async def test_find_sends_the_identifier_as_a_query() -> None:
    backend, seen = _backend(lambda r: httpx.Response(200, json=_person()))

    await backend.find_user("nino@suliko.ge")

    assert seen[0].url.path == "/api/office/users/lookup"
    assert seen[0].url.params["identifier"] == "nino@suliko.ge"


async def test_an_id_with_odd_characters_stays_in_its_path_segment() -> None:
    backend, seen = _backend(lambda r: httpx.Response(200, json=_person()))

    await backend.get_user("a/b?c")

    assert seen[0].url.raw_path.decode().startswith("/api/office/users/a%2Fb%3Fc")


async def test_the_whole_directory_is_walked_page_by_page() -> None:
    rows = [_person(id=f"g{n}", userName=f"p{n}@suliko.ge") for n in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        size = int(request.url.params["pageSize"])
        chunk = rows[(page - 1) * size : page * size]
        return httpx.Response(200, json={"total": len(rows), "items": chunk})

    backend, seen = _backend(handler)

    people = [person async for person in backend.iter_users(page_size=2)]

    assert [p.id for p in people] == ["g0", "g1", "g2", "g3", "g4"]
    assert len(seen) == 3


async def test_an_empty_directory_ends_at_once() -> None:
    backend, seen = _backend(lambda r: httpx.Response(200, json={"total": 0, "items": []}))

    assert [p async for p in backend.iter_users()] == []
    assert len(seen) == 1


async def test_a_list_that_stops_short_of_its_total_does_not_loop_forever() -> None:
    backend, seen = _backend(lambda r: httpx.Response(200, json={"total": 9, "items": []}))

    assert [p async for p in backend.iter_users()] == []
    assert len(seen) == 1


async def test_a_directory_that_changes_shape_is_unavailable() -> None:
    backend, _ = _backend(lambda r: httpx.Response(200, json={"people": []}))

    with pytest.raises(SulikoUnavailableError):
        _ = [p async for p in backend.iter_users()]


# ── Reading a person ────────────────────────────────────────────────────────


def test_an_email_sign_in_is_an_address_lower_cased() -> None:
    person = parse_user(_person(userName="  Nino@Suliko.GE "))

    assert person.signs_in_with_email
    assert person.email == "nino@suliko.ge"
    assert person.phone is None
    assert person.full_name == "Nino Beridze"


def test_a_phone_sign_in_is_a_phone_and_no_address() -> None:
    person = parse_user(_person(userName="599123456", firstName="", lastName="Gela"))

    assert not person.signs_in_with_email
    assert person.email is None
    assert person.phone == "599123456"
    assert person.full_name == "Gela"


def test_the_serialisers_casing_does_not_matter() -> None:
    person = parse_user({"Id": "g", "UserName": "a@b.ge", "FirstName": "A", "UserType": "Google"})

    assert (person.id, person.user_name, person.first_name, person.user_type) == (
        "g",
        "a@b.ge",
        "A",
        "google",
    )


def test_a_creation_time_that_will_not_parse_is_just_missing() -> None:
    assert parse_user(_person(createdAt="yesterday")).created_at is None
    assert parse_user(_person()).created_at is not None


@pytest.mark.parametrize("data", [None, [], "x", {}, {"id": "", "userName": "a@b.ge"}, {"id": "g"}])
def test_something_that_is_not_a_person_is_refused(data: Any) -> None:
    with pytest.raises(SulikoUnavailableError):
        parse_user(data)


# ── Not connected ───────────────────────────────────────────────────────────


# ── Sign-in on suliko.ge ────────────────────────────────────────────────────


async def test_a_code_is_redeemed_with_the_key_for_the_person() -> None:
    backend, seen = _backend(lambda r: httpx.Response(200, json=_person()))

    found = await backend.redeem_sso_code("the-code", "v" * 43, "https://app.example/cb")

    assert found is not None and found.id == "guid-1"
    request = seen[0]
    assert request.method == "POST" and request.url.path == "/api/office/sso/redeem"
    assert request.headers[KEY_HEADER] == KEY
    assert json.loads(request.content) == {
        "code": "the-code",
        "codeVerifier": "v" * 43,
        "redirectUri": "https://app.example/cb",
    }


async def test_a_refused_code_is_nobody() -> None:
    backend, _ = _backend(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))

    assert await backend.redeem_sso_code("used", "v" * 43, "https://app.example/cb") is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"errors": {"code": ["required"]}}),
        httpx.Response(401),
        httpx.Response(404),
        httpx.Response(500),
        httpx.Response(200, text="not json"),
    ],
)
async def test_anything_else_from_redeem_is_unavailable(response: httpx.Response) -> None:
    """A 404 is a suliko.ge without the endpoint yet, a 401 a wrong key: setup
    problems, never "this person may not sign in"."""
    backend, _ = _backend(lambda r: response)

    with pytest.raises(SulikoUnavailableError):
        await backend.redeem_sso_code("c", "v" * 43, "https://app.example/cb")


async def test_an_unreachable_suliko_cannot_redeem() -> None:
    backend, _ = _backend(lambda r: httpx.ConnectError("refused"))

    with pytest.raises(SulikoUnavailableError):
        await backend.redeem_sso_code("c", "v" * 43, "https://app.example/cb")


async def test_without_a_key_no_code_is_sent() -> None:
    backend, seen = _backend(lambda r: httpx.Response(200, json=_person()), key="")

    with pytest.raises(SulikoUnavailableError):
        await backend.redeem_sso_code("c", "v" * 43, "https://app.example/cb")
    assert seen == []


async def test_an_unconfigured_backend_vouches_for_nobody() -> None:
    backend = UnconfiguredSulikoBackend()

    assert not backend.enabled
    assert (await backend.check_password("a@b.ge", "pw")).outcome is PasswordOutcome.REJECTED
    with pytest.raises(SulikoUnavailableError):
        await backend.find_user("a@b.ge")
    with pytest.raises(SulikoUnavailableError):
        backend.iter_users()
    with pytest.raises(SulikoUnavailableError):
        await backend.redeem_sso_code("c", "v" * 43, "https://app.example/cb")
