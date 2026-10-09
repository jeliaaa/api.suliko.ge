"""A stand-in for suliko.ge's backend, for tests that need one.

`FakeBackend` answers from a dict, so every branch of the sign-in and invite
decisions can be reached without a network: a right password, a wrong one, a
person suliko.ge has never heard of, and suliko.ge being down.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from suliko.integrations.suliko_backend import (
    PasswordCheck,
    PasswordOutcome,
    SulikoContact,
    SulikoUnavailableError,
    SulikoUser,
)


def person(
    suliko_id: str = "g1",
    user_name: str = "nino@suliko.ge",
    first: str = "Nino",
    last: str = "Beridze",
) -> SulikoUser:
    return SulikoUser(
        id=suliko_id,
        user_name=user_name,
        first_name=first,
        last_name=last,
        user_type="normal",
        created_at=None,
    )


def _digits(raw: str | None) -> str | None:
    """A phone number as the real search compares it (`UserService.NormalizePhone`
    on suliko.ge's backend): digits only, no 995 prefix, no leading trunk zero."""
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits.startswith("995") and len(digits) == 12:
        digits = digits[3:]
    elif len(digits) == 10 and digits.startswith("05"):
        digits = digits[1:]
    return digits if len(digits) >= 6 else None


def contact(
    suliko_id: str = "g1",
    user_name: str = "nino@suliko.ge",
    first: str = "Nino",
    last: str = "Beridze",
    *,
    email: str | None = None,
    phone: str | None = None,
) -> SulikoContact:
    """A search result: the profile email and phone are given separately from the
    sign-in name, as they are on suliko.ge."""
    return SulikoContact(
        id=suliko_id,
        user_name=user_name,
        first_name=first,
        last_name=last,
        user_type="normal",
        email=email,
        phone=phone,
    )


class FakeBackend:
    """suliko.ge, from a dict. `passwords` maps a suliko id to its password."""

    def __init__(
        self,
        people: list[SulikoUser] | None = None,
        passwords: dict[str, str] | None = None,
        *,
        enabled: bool = True,
        down: bool = False,
        directory_down: bool = False,
        codes: dict[str, str] | None = None,
        contacts: list[SulikoContact] | None = None,
    ) -> None:
        self.people = people or []
        #: What a search can find (profile email/phone kept apart from the sign-in name).
        self.contacts = contacts or []
        self.searches: list[str] = []
        #: One-time sign-in codes: code -> suliko id. Spent on first use.
        self.codes = codes or {}
        self.passwords = passwords or {}
        self._enabled = enabled
        self.down = down
        self.directory_down = directory_down
        self.directory_calls = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def check_password(self, login: str, password: str) -> PasswordCheck:
        if self.down:
            return PasswordCheck(PasswordOutcome.UNAVAILABLE)
        for p in self.people:
            same_login = p.user_name.lower() == login.strip().lower()
            if same_login and self.passwords.get(p.id) == password:
                return PasswordCheck(PasswordOutcome.ACCEPTED, p.id)
        return PasswordCheck(PasswordOutcome.REJECTED)

    def _guard(self) -> None:
        self.directory_calls += 1
        if self.directory_down:
            raise SulikoUnavailableError("down")

    async def get_user(self, user_id: str) -> SulikoUser | None:
        self._guard()
        return next((p for p in self.people if p.id == user_id), None)

    async def find_user(self, login: str) -> SulikoUser | None:
        self._guard()
        return next((p for p in self.people if p.user_name.lower() == login.strip().lower()), None)

    async def search_users(self, query: str) -> list[SulikoContact]:
        """Equality on the email or the number, as the real search does."""
        self._guard()
        self.searches.append(query)
        wanted = query.strip().lower()
        digits = _digits(query) if "@" not in query else None
        found = []
        for c in self.contacts:
            by_email = wanted in {(c.email or "").lower(), c.user_name.lower()}
            by_phone = digits is not None and digits in {
                _digits(c.phone),
                _digits(c.user_name if "@" not in c.user_name else None),
            }
            if by_email or by_phone:
                found.append(c)
        return found

    async def iter_users(self, page_size: int = 200) -> AsyncIterator[SulikoUser]:
        self._guard()
        for p in self.people:
            yield p

    async def redeem_sso_code(
        self, code: str, code_verifier: str, redirect_uri: str
    ) -> SulikoUser | None:
        if self.down:
            raise SulikoUnavailableError("down")
        suliko_id = self.codes.pop(code, None)
        return next((p for p in self.people if p.id == suliko_id), None)

    async def aclose(self) -> None:
        return None
