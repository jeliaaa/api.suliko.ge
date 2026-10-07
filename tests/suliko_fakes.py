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
    ) -> None:
        self.people = people or []
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

    async def iter_users(self, page_size: int = 200) -> AsyncIterator[SulikoUser]:
        self._guard()
        for p in self.people:
            yield p

    async def aclose(self) -> None:
        return None
