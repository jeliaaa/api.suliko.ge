"""People from suliko.ge as accounts here: the one rule, sign-in, search, import.

In-memory SQLite, like ``test_accounts.py``. suliko.ge is a fake that answers
from a dict, so every branch of the sign-in decision can be reached without a
network: a right password, a wrong one, a person suliko.ge has never heard of,
and suliko.ge being down.

What is NOT covered: the endpoints' own database sessions (they open their
own, and need PostgreSQL), so their ordering rules are pinned by reading the
source, as ``test_tenant_access.py`` does for the same reason.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.api.v1 import auth, users
from suliko.core.errors import ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import install_tenant_filter
from suliko.domain.accounts import (
    UNUSABLE_PASSWORD_HASH,
    SulikoAccountConflictError,
    create_personal_workspace,
    find_account,
    find_account_by_login,
    find_account_by_suliko_id,
    find_suliko_person,
    login_name,
    phone_variants,
    upsert_from_suliko,
    username_for_account,
)
from suliko.domain.suliko_import import import_users, link_by_hand
from suliko.integrations.suliko_backend import SulikoUnavailableError, SulikoUser
from suliko.models.reference import DocumentType, Language, LanguagePairPrice, TenantSettings
from suliko.models.tenant import Tenant
from suliko.models.user import Account, Role, User
from suliko.security.passwords import hash_password, verify_password
from suliko_fakes import FakeBackend, person

TABLES = [
    Tenant.__table__,
    Account.__table__,
    User.__table__,
    TenantSettings.__table__,
    Language.__table__,
    DocumentType.__table__,
    LanguagePairPrice.__table__,
]

LOCAL_PASSWORD = "an-office-only-password"


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _office_account(
    db: AsyncSession, email: str = "nino@suliko.ge", password: str = LOCAL_PASSWORD
) -> Account:
    account = Account(
        email=email, password_hash=hash_password(password), full_name="Nino From Office"
    )
    db.add(account)
    await db.flush()
    return account


# ── The one rule ────────────────────────────────────────────────────────────


async def test_a_new_email_person_gets_an_account_with_no_password_of_its_own(
    db: AsyncSession,
) -> None:
    link = await upsert_from_suliko(db, person())

    account = link.account
    assert link.created and not link.linked_existing
    assert (account.email, account.phone, account.suliko_user_id) == ("nino@suliko.ge", None, "g1")
    assert account.full_name == "Nino Beridze"
    assert account.password_hash == UNUSABLE_PASSWORD_HASH
    assert not verify_password("", account.password_hash)
    # suliko.ge proved the address at registration.
    assert account.email_verified_at is not None


async def test_a_new_phone_person_has_a_phone_and_no_address(db: AsyncSession) -> None:
    link = await upsert_from_suliko(db, person("g2", "599123456", "Gela", ""))

    account = link.account
    assert (account.email, account.phone) == (None, "599123456")
    assert account.full_name == "Gela"
    assert account.email_verified_at is None
    assert login_name(account) == "599123456"
    assert username_for_account(account) == "599123456"


async def test_a_person_with_no_name_is_called_by_their_sign_in(db: AsyncSession) -> None:
    link = await upsert_from_suliko(db, person("g3", "599000111", "", ""))

    assert link.account.full_name == "599000111"


async def test_the_same_person_twice_is_the_same_account(db: AsyncSession) -> None:
    first = await upsert_from_suliko(db, person())
    second = await upsert_from_suliko(db, person())

    assert first.account.id == second.account.id
    assert not second.created and not second.linked_existing
    assert len((await db.execute(select(Account))).scalars().all()) == 1


async def test_a_person_is_found_by_id_even_if_their_sign_in_changed(db: AsyncSession) -> None:
    first = await upsert_from_suliko(db, person("g1", "old@suliko.ge"))
    again = await upsert_from_suliko(db, person("g1", "new@suliko.ge"))

    assert again.account.id == first.account.id


async def test_an_existing_office_account_with_that_address_is_linked(db: AsyncSession) -> None:
    office = await _office_account(db)

    link = await upsert_from_suliko(db, person())

    assert link.linked_existing and not link.created
    assert link.account.id == office.id
    assert office.suliko_user_id == "g1"
    # Its own password is gone, not just ignored: unlinking cannot bring it back.
    assert office.password_hash == UNUSABLE_PASSWORD_HASH
    assert not verify_password(LOCAL_PASSWORD, office.password_hash)
    assert office.email_verified_at is not None
    assert office.full_name == "Nino From Office"  # its own name is kept


async def test_the_address_match_ignores_case(db: AsyncSession) -> None:
    office = await _office_account(db, "nino@suliko.ge")

    link = await upsert_from_suliko(db, person("g1", "  Nino@SULIKO.ge "))

    assert link.account.id == office.id


async def test_an_address_linked_to_someone_else_is_a_conflict_not_a_takeover(
    db: AsyncSession,
) -> None:
    office = await _office_account(db)
    office.suliko_user_id = "somebody-else"
    await db.flush()

    with pytest.raises(SulikoAccountConflictError):
        await upsert_from_suliko(db, person("g1", "nino@suliko.ge"))
    assert office.suliko_user_id == "somebody-else"


async def test_a_phone_person_never_matches_an_office_account_by_address(
    db: AsyncSession,
) -> None:
    office = await _office_account(db, "gela@suliko.ge")

    link = await upsert_from_suliko(db, person("g9", "599111222", "Gela", "X"))

    assert link.created and link.account.id != office.id


async def test_lookups_by_login(db: AsyncSession) -> None:
    email = await upsert_from_suliko(db, person("g1", "nino@suliko.ge"))
    phone = await upsert_from_suliko(db, person("g2", "599123456", "Gela", ""))

    assert (await find_account_by_login(db, "Nino@Suliko.GE")) is email.account
    assert (await find_account_by_login(db, " 599123456 ")) is phone.account
    assert await find_account_by_login(db, "other@suliko.ge") is None
    assert await find_account_by_login(db, "599000000") is None
    assert (await find_account_by_suliko_id(db, "g2")) is phone.account
    assert await find_account_by_suliko_id(db, "nope") is None
    assert await find_account(db, "nino@suliko.ge") is email.account


async def test_a_phone_only_person_can_open_a_personal_account(db: AsyncSession) -> None:
    account = (await upsert_from_suliko(db, person("g2", "599123456", "Gela", "Beridze"))).account

    created = await create_personal_workspace(db, account)

    assert created.tenant.is_personal
    assert created.tenant.display_name == "Gela Beridze"
    assert created.user.role is Role.OWNER
    assert created.user.email is None
    assert created.user.username == "599123456"
    assert created.user.account_id == account.id


# ── Signing in ──────────────────────────────────────────────────────────────


async def test_a_right_suliko_password_signs_in_and_makes_the_account(db: AsyncSession) -> None:
    backend = FakeBackend([person()], {"g1": "suliko-pass"})

    account, unavailable = await auth._authenticate(db, backend, "nino@suliko.ge", "suliko-pass")

    assert account is not None and not unavailable
    assert account.suliko_user_id == "g1"


async def test_a_known_person_signs_in_without_asking_the_directory(db: AsyncSession) -> None:
    await upsert_from_suliko(db, person())
    backend = FakeBackend([person()], {"g1": "suliko-pass"}, directory_down=True)

    account, unavailable = await auth._authenticate(db, backend, "nino@suliko.ge", "suliko-pass")

    assert account is not None and not unavailable
    assert backend.directory_calls == 0


async def test_a_new_person_cannot_sign_in_while_the_directory_is_down(db: AsyncSession) -> None:
    backend = FakeBackend([person()], {"g1": "suliko-pass"}, directory_down=True)

    with pytest.raises(SulikoUnavailableError):
        await auth._authenticate(db, backend, "nino@suliko.ge", "suliko-pass")
    assert await find_account_by_suliko_id(db, "g1") is None


async def test_a_person_suliko_has_already_described_is_not_asked_about_again(
    db: AsyncSession,
) -> None:
    """Sign-in by code: suliko.ge's answer already says who they are."""
    backend = FakeBackend([person()], directory_down=True)

    account = await auth._account_for_suliko_user(db, backend, "g1", person())

    assert account.suliko_user_id == "g1" and account.email == "nino@suliko.ge"
    assert backend.directory_calls == 0


async def test_a_wrong_suliko_password_signs_nobody_in(db: AsyncSession) -> None:
    await upsert_from_suliko(db, person())
    backend = FakeBackend([person()], {"g1": "suliko-pass"})

    assert await auth._authenticate(db, backend, "nino@suliko.ge", "nope") == (None, False)


async def test_nothing_a_suliko_person_types_works_against_the_placeholder_hash(
    db: AsyncSession,
) -> None:
    """The stored hash of a linked account is not a password anyone can type —
    not the placeholder itself, which is what a careless comparison would accept."""
    await upsert_from_suliko(db, person())
    backend = FakeBackend([person()], {"g1": "suliko-pass"})

    assert await auth._authenticate(db, backend, "nino@suliko.ge", UNUSABLE_PASSWORD_HASH) == (
        None,
        False,
    )


async def test_an_unknown_login_signs_nobody_in(db: AsyncSession) -> None:
    backend = FakeBackend([person()], {"g1": "suliko-pass"})

    assert await auth._authenticate(db, backend, "stranger@suliko.ge", "x") == (None, False)


async def test_an_office_only_account_signs_in_on_its_own_password(db: AsyncSession) -> None:
    office = await _office_account(db, "operator@suliko.ge")
    backend = FakeBackend([person()], {"g1": "suliko-pass"})

    got, unavailable = await auth._authenticate(db, backend, "Operator@Suliko.ge", LOCAL_PASSWORD)

    assert got is office and not unavailable
    assert await auth._authenticate(db, backend, "operator@suliko.ge", "wrong") == (None, False)


async def test_once_linked_the_old_office_password_is_dead(db: AsyncSession) -> None:
    await _office_account(db)
    backend = FakeBackend([person()], {"g1": "suliko-pass"})

    # Signing in with the suliko.ge password links the account...
    account, _ = await auth._authenticate(db, backend, "nino@suliko.ge", "suliko-pass")
    assert account is not None and account.suliko_user_id == "g1"
    # ...and from then on the old one gets nobody in.
    assert await auth._authenticate(db, backend, "nino@suliko.ge", LOCAL_PASSWORD) == (None, False)


async def test_suliko_down_is_unavailable_not_wrong_password(db: AsyncSession) -> None:
    await upsert_from_suliko(db, person())
    backend = FakeBackend([person()], {"g1": "suliko-pass"}, down=True)

    assert await auth._authenticate(db, backend, "nino@suliko.ge", "suliko-pass") == (None, True)
    assert await auth._authenticate(db, backend, "stranger@suliko.ge", "x") == (None, True)


async def test_suliko_down_still_lets_an_office_only_account_in(db: AsyncSession) -> None:
    office = await _office_account(db, "operator@suliko.ge")
    backend = FakeBackend([], down=True)

    got, unavailable = await auth._authenticate(db, backend, "operator@suliko.ge", LOCAL_PASSWORD)
    assert got is office and not unavailable


async def test_suliko_down_and_a_wrong_office_password_is_unavailable(db: AsyncSession) -> None:
    """The password may have been their suliko.ge one, which cannot be checked now."""
    await _office_account(db, "operator@suliko.ge")
    backend = FakeBackend([], down=True)

    assert await auth._authenticate(db, backend, "operator@suliko.ge", "wrong") == (None, True)


async def test_with_suliko_disconnected_only_office_passwords_count(db: AsyncSession) -> None:
    office = await _office_account(db)
    backend = FakeBackend([person()], {"g1": "suliko-pass"}, enabled=False)

    assert (await auth._authenticate(db, backend, "nino@suliko.ge", LOCAL_PASSWORD))[0] is office
    assert await auth._authenticate(db, backend, "nino@suliko.ge", "suliko-pass") == (None, False)


async def test_two_first_sign_ins_racing_end_with_the_winners_account(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unique index on the suliko id picks a winner; the loser must find the
    winner's account, not fail."""
    backend = FakeBackend([person()], {"g1": "suliko-pass"})

    async def lost_the_race(session: AsyncSession, who: SulikoUser) -> Any:
        winner = Account(
            email=who.email,
            suliko_user_id=who.id,
            password_hash=UNUSABLE_PASSWORD_HASH,
            full_name="Winner",
        )
        session.add(winner)
        await session.commit()
        raise IntegrityError("insert", {}, Exception("unique"))

    monkeypatch.setattr(auth, "upsert_from_suliko", lost_the_race)

    account, unavailable = await auth._authenticate(db, backend, "nino@suliko.ge", "suliko-pass")

    assert account is not None and account.full_name == "Winner" and not unavailable


# ── The endpoint's ordering rules ───────────────────────────────────────────


def test_suliko_being_down_is_not_counted_against_the_person() -> None:
    source = inspect.getsource(auth.login)
    unavailable_branch = source[
        source.index("if unavailable:") : source.index("if account is None:")
    ]
    assert "record_login_failure" not in unavailable_branch
    assert "SulikoUnavailableError" in unavailable_branch


def test_login_asks_for_the_backend_and_still_takes_the_old_field_name() -> None:
    assert "get_suliko_backend" in inspect.getsource(auth.login)
    assert auth.LoginRequest(email="a@b.ge", password="x").login == "a@b.ge"
    assert auth.LoginRequest(identifier=" 599123456 ", password="x").login == "599123456"
    assert auth.LoginRequest(identifier="a", email="b", password="x").login == "a"
    with pytest.raises(ValueError, match="identifier"):
        auth.LoginRequest(password="x")
    with pytest.raises(ValueError, match="identifier"):
        auth.LoginRequest(identifier="   ", password="x")


def test_a_suliko_password_cannot_be_changed_here() -> None:
    source = inspect.getsource(auth.change_password)
    assert source.index("PasswordManagedExternallyError") < source.index("verify_and_maybe_rehash")
    source = inspect.getsource(auth.reset_password)
    assert source.index("PasswordManagedExternallyError") < source.index("hash_password_async")
    assert auth.PasswordManagedExternallyError.error_code == "password_managed_by_suliko"


def test_forgot_password_for_a_suliko_person_issues_no_token() -> None:
    source = inspect.getsource(auth.forgot_password)
    assert source.index("_suliko_password_email") < source.index("reset_tokens.issue")


def test_the_suliko_reset_email_says_where_to_go_and_offers_no_local_link() -> None:
    account = Account(email="nino@suliko.ge", full_name="Nino", password_hash="!")

    subject, body = auth._suliko_password_email(account, "https://suliko.ge/login")

    assert "https://suliko.ge/login" in body
    assert "kept on suliko.ge" in body
    # Someone who has not registered there yet is pointed at registration.
    assert "https://suliko.ge/register" in body
    assert "reset-password" not in body
    assert "Nino" in body and subject


def test_an_invitee_with_no_password_is_never_given_a_local_reset_link() -> None:
    """A placeholder account (invited, not yet registered on suliko.ge) holds no
    password. A reset link of ours would give it one that exists only here."""
    source = inspect.getsource(auth.forgot_password)
    guard = source[: source.index("_suliko_password_email")]
    assert "password_hash == UNUSABLE_PASSWORD_HASH" in guard
    assert "reset_tokens.issue" not in guard


def test_no_address_means_nothing_to_verify() -> None:
    from suliko.security import sessions

    source = inspect.getsource(sessions.resolve_session)
    assert "not has_address or" in source


# ── Searching for someone to invite ─────────────────────────────────────────


async def _lookup(db: AsyncSession, login: str, backend: FakeBackend) -> Any:
    return await users.lookup_account(
        db=db,
        session=None,  # type: ignore[arg-type]
        _=None,
        identifier=login,
        backend=backend,
    )


async def test_someone_only_on_suliko_is_found_without_being_created(db: AsyncSession) -> None:
    result = await _lookup(db, "nino@suliko.ge", FakeBackend([person()]))

    assert (result.exists, result.full_name, result.member, result.pending) == (
        True,
        "Nino Beridze",
        False,
        False,
    )
    assert (await db.execute(select(Account))).scalars().all() == []


async def test_someone_nowhere_is_not_found(db: AsyncSession) -> None:
    result = await _lookup(db, "nobody@suliko.ge", FakeBackend([person()]))

    assert result.exists is False
    # With suliko.ge connected, the invitation will ask them to register there.
    assert result.registration_required is True


async def test_with_suliko_disconnected_only_office_accounts_are_found(db: AsyncSession) -> None:
    result = await _lookup(db, "nino@suliko.ge", FakeBackend([person()], enabled=False))

    assert result.exists is False
    assert result.registration_required is False


async def test_a_search_while_suliko_is_down_fails_rather_than_saying_nobody(
    db: AsyncSession,
) -> None:
    with pytest.raises(SulikoUnavailableError):
        await _lookup(db, "nino@suliko.ge", FakeBackend([person()], directory_down=True))


async def test_an_office_account_is_found_without_asking_suliko(db: AsyncSession) -> None:
    await _office_account(db)
    backend = FakeBackend([], directory_down=True)

    result = await _lookup(db, "nino@suliko.ge", backend)

    assert result.exists and result.full_name == "Nino From Office"
    assert backend.directory_calls == 0


def _invitee() -> User:
    return User(
        username="new@suliko.ge",
        email="new@suliko.ge",
        full_name="New Person",
        password_hash="!",
        role=Role.STAFF,
    )


def _no_suliko_account() -> Any:
    from suliko.api.v1.users import SulikoAccountOut

    return SulikoAccountOut(status="pending", matched_display_name=None)


def test_the_invitation_to_a_stranger_says_to_register_first() -> None:
    subject, body = users._invite_email(
        _invitee(),
        "Acme",
        "https://app.example/ka/accept-invite?token=T",
        "Boss",
        7,
        existing_account=False,
        suliko_account=_no_suliko_account(),
        needs_registration=True,
    )

    assert "Acme" in subject
    assert "Register on suliko.ge with exactly this email address (new@suliko.ge)" in body
    assert "https://suliko.ge/register" in body
    assert "accept-invite?token=T" in body
    assert body.index("Register on suliko.ge") < body.index("accept-invite")
    # The one-time-password flow is gone for them.
    assert "Choose your password" not in body
    assert "reset-password" not in body


def test_the_other_invitations_are_as_they_were() -> None:
    kwargs: dict[str, Any] = {"suliko_account": _no_suliko_account()}
    _, existing = users._invite_email(
        _invitee(), "Acme", "L", "Boss", 7, existing_account=True, **kwargs
    )
    _, fresh = users._invite_email(
        _invitee(), "Acme", "L", "Boss", 7, existing_account=False, **kwargs
    )

    assert "Accept the invitation here" in existing
    assert "Choose your password here" in fresh


def test_invite_resolves_people_through_suliko_before_creating_a_placeholder() -> None:
    source = inspect.getsource(users.invite_user)
    assert source.index("find_suliko_person") < source.index("clash = (")
    assert source.index("upsert_from_suliko") < source.index("needs_registration = ")
    assert "UNUSABLE_PASSWORD_HASH" in source


# ── The import ──────────────────────────────────────────────────────────────


def _everyone() -> list[SulikoUser]:
    return [
        person("g1", "nino@suliko.ge"),
        person("g2", "599123456", "Gela", ""),
        person("g3", "tako@suliko.ge", "Tako", "Mgeladze"),
    ]


async def test_a_dry_run_writes_nothing_and_says_what_would_happen(db: AsyncSession) -> None:
    await _office_account(db, "tako@suliko.ge")
    await _office_account(db, "operator@suliko.ge")
    await db.commit()

    report = await import_users(db, FakeBackend(_everyone()), dry_run=True)
    await db.rollback()

    assert report.seen == 3
    assert report.created == 2  # nino and the phone person
    assert report.linked_existing == ["tako@suliko.ge"]
    assert report.office_only == ["operator@suliko.ge"]
    accounts = (await db.execute(select(Account))).scalars().all()
    assert {a.email for a in accounts} == {"tako@suliko.ge", "operator@suliko.ge"}
    assert all(a.suliko_user_id is None for a in accounts)


async def test_a_real_run_does_what_the_dry_run_said(db: AsyncSession) -> None:
    await _office_account(db, "tako@suliko.ge")
    await _office_account(db, "operator@suliko.ge")
    await db.commit()
    dry = await import_users(db, FakeBackend(_everyone()), dry_run=True)
    await db.rollback()

    real = await import_users(db, FakeBackend(_everyone()), dry_run=False)
    await db.commit()

    assert (real.seen, real.created) == (dry.seen, dry.created)
    assert real.linked_existing == dry.linked_existing
    assert real.office_only == dry.office_only == ["operator@suliko.ge"]
    assert (await find_account(db, "tako@suliko.ge")).suliko_user_id == "g3"  # type: ignore[union-attr]
    assert (await find_account_by_login(db, "599123456")).suliko_user_id == "g2"  # type: ignore[union-attr]


async def test_running_the_import_again_finds_nothing_to_do(db: AsyncSession) -> None:
    await import_users(db, FakeBackend(_everyone()), dry_run=False)
    await db.commit()

    again = await import_users(db, FakeBackend(_everyone()), dry_run=False)

    assert (again.seen, again.already_linked, again.created) == (3, 3, 0)
    assert again.linked_existing == [] and again.conflicts == []


async def test_a_later_registration_is_the_only_thing_a_second_run_adds(
    db: AsyncSession,
) -> None:
    await import_users(db, FakeBackend(_everyone()), dry_run=False)
    await db.commit()
    newcomer = person("g4", "new@suliko.ge", "New", "Person")

    again = await import_users(db, FakeBackend([*_everyone(), newcomer]), dry_run=False)

    assert (again.already_linked, again.created) == (3, 1)


async def test_a_sign_in_two_people_claim_is_reported_and_left_alone(db: AsyncSession) -> None:
    office = await _office_account(db)
    office.suliko_user_id = "somebody-else"
    await db.commit()

    report = await import_users(db, FakeBackend([person("g1", "nino@suliko.ge")]), dry_run=False)

    assert report.conflicts == ["nino@suliko.ge"] and report.created == 0
    assert office.suliko_user_id == "somebody-else"


async def test_an_import_with_suliko_unreachable_changes_nothing(db: AsyncSession) -> None:
    with pytest.raises(SulikoUnavailableError):
        await import_users(db, FakeBackend(_everyone(), directory_down=True), dry_run=False)
    assert (await db.execute(select(Account))).scalars().all() == []


# ── Linking one account by hand ─────────────────────────────────────────────


async def test_an_owner_whose_suliko_sign_in_is_a_phone_is_linked_by_hand(
    db: AsyncSession,
) -> None:
    office = await _office_account(db, "owner@office.ge")
    backend = FakeBackend([person("g2", "599123456", "Gela", "Owner")])

    message = await link_by_hand(
        db, backend, office_email="owner@office.ge", suliko_login="599123456"
    )

    assert office.suliko_user_id == "g2"
    assert office.phone == "599123456"
    assert office.email == "owner@office.ge"  # still found by address for invitations
    assert office.password_hash == UNUSABLE_PASSWORD_HASH
    assert "owner@office.ge" in message and "599123456" in message
    assert await find_account_by_login(db, "599123456") is office


async def test_linking_by_hand_removes_the_empty_duplicate_the_import_made(
    db: AsyncSession,
) -> None:
    office = await _office_account(db, "owner@office.ge")
    backend = FakeBackend([person("g2", "599123456", "Gela", "Owner")])
    duplicate = (await upsert_from_suliko(db, backend.people[0])).account
    assert duplicate.id != office.id

    message = await link_by_hand(
        db, backend, office_email="owner@office.ge", suliko_login="599123456"
    )

    assert "Removed the empty account" in message
    assert await db.get(Account, duplicate.id) is None
    assert office.suliko_user_id == "g2"


async def test_linking_by_hand_never_discards_an_account_that_has_organisations(
    db: AsyncSession,
) -> None:
    await _office_account(db, "owner@office.ge")
    backend = FakeBackend([person("g2", "599123456", "Gela", "Owner")])
    duplicate = (await upsert_from_suliko(db, backend.people[0])).account
    await create_personal_workspace(db, duplicate)
    await db.flush()

    with pytest.raises(ValidationError, match="organisation"):
        await link_by_hand(db, backend, office_email="owner@office.ge", suliko_login="599123456")
    assert await db.get(Account, duplicate.id) is not None


@pytest.mark.parametrize(
    ("email", "login", "fragment"),
    [
        ("nobody@office.ge", "599123456", "No Office account"),
        ("owner@office.ge", "599000000", "nobody who signs in"),
    ],
)
async def test_linking_by_hand_refuses_what_does_not_exist(
    db: AsyncSession, email: str, login: str, fragment: str
) -> None:
    await _office_account(db, "owner@office.ge")
    backend = FakeBackend([person("g2", "599123456", "Gela", "Owner")])

    with pytest.raises(ValidationError, match=fragment):
        await link_by_hand(db, backend, office_email=email, suliko_login=login)


async def test_linking_by_hand_refuses_an_account_that_is_already_linked(
    db: AsyncSession,
) -> None:
    office = await _office_account(db, "owner@office.ge")
    office.suliko_user_id = "g7"
    await db.flush()

    with pytest.raises(ValidationError, match="already linked"):
        await link_by_hand(
            db,
            FakeBackend([person("g2", "599123456")]),
            office_email="owner@office.ge",
            suliko_login="599123456",
        )


# ── Configuration ───────────────────────────────────────────────────────────


def _prod_settings(**overrides: object) -> Any:
    from suliko.config import Settings

    base: dict[str, object] = {
        "environment": "production",
        "debug": False,
        "db_echo": False,
        "encryption_master_key": "a" * 44,
        "redis_url": "redis://localhost:6379/0",
        "cors_origins": ["https://app.suliko.ge"],
        "smtp_host": "smtp.example.com",
        "smtp_from_email": "noreply@suliko.ge",
        "app_url": "https://app.suliko.ge",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_suliko_is_off_unless_a_url_is_set() -> None:
    assert not _prod_settings().suliko_backend_enabled
    assert not _prod_settings(suliko_api_url="   ").suliko_backend_enabled
    _prod_settings().validate_for_production()


def test_production_accepts_a_complete_suliko_setup() -> None:
    settings = _prod_settings(suliko_api_url="https://content.api24.ge", suliko_api_key="k" * 32)

    assert settings.suliko_backend_enabled
    settings.validate_for_production()


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"suliko_api_url": "http://content.api24.ge", "suliko_api_key": "k" * 32},
            "SULIKO_API_URL",
        ),
        ({"suliko_api_url": "https://content.api24.ge", "suliko_api_key": ""}, "SULIKO_API_KEY"),
        (
            {"suliko_api_url": "https://content.api24.ge", "suliko_api_key": "short"},
            "SULIKO_API_KEY",
        ),
    ],
)
def test_production_refuses_a_half_configured_suliko(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(RuntimeError, match=expected):
        _prod_settings(**overrides).validate_for_production()


# ── Phone numbers ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("599123456", ["599123456", "+995599123456", "995599123456"]),
        (
            "+995 599 12 34 56",
            ["+995 599 12 34 56", "995599123456", "599123456", "+995599123456"],
        ),
        ("0599123456", ["0599123456", "599123456", "+995599123456", "995599123456"]),
        ("12345", ["12345"]),
        ("", []),
        ("   ", []),
    ],
)
def test_a_phone_number_is_tried_in_each_form_it_may_be_stored(
    raw: str, expected: list[str]
) -> None:
    assert phone_variants(raw) == expected


async def test_a_phone_account_is_found_however_the_number_is_typed(db: AsyncSession) -> None:
    plain = (await upsert_from_suliko(db, person("g2", "599123456", "Gela", ""))).account
    prefixed = (await upsert_from_suliko(db, person("g3", "+995577000111", "Tako", ""))).account

    for typed in ("599123456", "+995 599 12 34 56", "0599123456", "995599123456"):
        assert await find_account_by_login(db, typed) is plain
    # Stored with the country code, typed without it.
    assert await find_account_by_login(db, "577 00 01 11") is prefixed
    assert await find_account_by_login(db, "599123457") is None


async def test_suliko_is_asked_about_each_form_of_a_number_until_found() -> None:
    backend = FakeBackend([person("g2", "+995599123456", "Gela", "")])

    found = await find_suliko_person(backend, "599 12 34 56")

    assert found is not None and found.id == "g2"
    assert (
        await find_suliko_person(FakeBackend([person("g2", "599123456", "Gela", "")]), "000")
        is None
    )


async def test_an_address_is_one_question_to_suliko() -> None:
    backend = FakeBackend([person()])

    assert (await find_suliko_person(backend, " Nino@Suliko.GE ")) is not None
    assert backend.directory_calls == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Nino@Suliko.GE ", "nino@suliko.ge"),
        ("599 12 34 56", "599 12 34 56"),
        ("+995599123456", "+995599123456"),
    ],
)
def test_the_search_and_the_invite_accept_an_address_or_a_number(raw: str, expected: str) -> None:
    assert users._checked_login(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "not-an-email@", "a@b", "12345", "abc"])
def test_anything_else_is_refused_with_a_422(raw: str | None) -> None:
    with pytest.raises(ValidationError):
        users._checked_login(raw)


def test_an_invite_needs_an_address_or_a_phone_number() -> None:
    users.UserInvite(full_name="A", email="a@b.ge")
    users.UserInvite(full_name="A", phone="599123456")
    users.UserInvite(full_name="A", email="a@b.ge", phone="599123456")
    with pytest.raises(ValueError, match="email address or a phone"):
        users.UserInvite(full_name="A")
    with pytest.raises(ValueError, match="email address or a phone"):
        users.UserInvite(full_name="A", phone="12")


async def test_a_phone_only_person_is_found_by_their_number(db: AsyncSession) -> None:
    backend = FakeBackend([person("g2", "599123456", "Gela", "Beridze")])

    result = await _lookup(db, "+995 599 12 34 56", backend)

    assert (result.exists, result.full_name, result.member) == (True, "Gela Beridze", False)
    assert (await db.execute(select(Account))).scalars().all() == []


async def test_a_number_nobody_uses_is_not_found(db: AsyncSession) -> None:
    result = await _lookup(db, "599000000", FakeBackend([person("g2", "599123456", "Gela", "")]))

    assert result.exists is False and result.registration_required is True


async def test_a_phone_account_here_is_found_without_asking_suliko(db: AsyncSession) -> None:
    await upsert_from_suliko(db, person("g2", "599123456", "Gela", "Beridze"))
    backend = FakeBackend([], directory_down=True)

    result = await _lookup(db, "0599123456", backend)

    assert result.exists and result.full_name == "Gela Beridze"
    assert backend.directory_calls == 0


@pytest.mark.parametrize("raw", ["abc", "12", "a@b"])
async def test_a_search_that_is_neither_is_refused(db: AsyncSession, raw: str) -> None:
    with pytest.raises(ValidationError):
        await _lookup(db, raw, FakeBackend([person()]))
