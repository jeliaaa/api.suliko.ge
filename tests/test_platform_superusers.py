"""Superusers named from suliko.ge accounts (`/platform/superusers`).

The one place platform-wide reach is handed out over HTTP, so what is pinned
is who can receive it (only a person suliko.ge knows), where it lands (the
organisation the acting superuser is signed in to, never one from the
request), and that it can be taken away again.

In-memory SQLite, like ``test_suliko_accounts.py``; suliko.ge is the fake from
``suliko_fakes``. The audit writer is recorded rather than run, as its table
is PostgreSQL-only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.api.v1 import platform
from suliko.core.errors import ConflictError, NotFoundError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.accounts import UNUSABLE_PASSWORD_HASH, memberships
from suliko.domain.plans import TenantPlan, effective_permissions
from suliko.integrations.suliko_backend import SulikoUnavailableError
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Account, Role, User, UserPermissionOverride
from suliko.security.passwords import hash_password
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession
from suliko_fakes import FakeBackend, person

#: The organisation the operator is signed in to, and somebody else's bureau.
PLATFORM, ACME = 1, 2
#: The operator: a superuser made on the server, with a password of its own.
OPERATOR, OPERATOR_ACCOUNT = 1, 1

TABLES = [
    Tenant.__table__,
    Account.__table__,
    User.__table__,
    UserPermissionOverride.__table__,
]


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest_asyncio.fixture
async def db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    audited: list[dict[str, Any]] = []

    async def record(_db: Any, _session: Any, **entry: Any) -> None:
        audited.append(entry)

    monkeypatch.setattr("suliko.core.audit.record", record)

    async with maker() as session:
        with bypass_tenant_scope():
            session.add_all(
                [
                    Tenant(
                        id=PLATFORM,
                        slug="suliko",
                        display_name="Suliko",
                        status=TenantStatus.ACTIVE,
                        plan="bureau",
                        locale="ka",
                    ),
                    Tenant(
                        id=ACME,
                        slug="acme",
                        display_name="Acme Translations",
                        status=TenantStatus.ACTIVE,
                        plan="bureau",
                        locale="ka",
                    ),
                    Account(
                        id=OPERATOR_ACCOUNT,
                        email="info@th.com.ge",
                        password_hash=hash_password("x" * 12),
                        full_name="The Operator",
                    ),
                ]
            )
            await session.flush()
            session.add(
                User(
                    id=OPERATOR,
                    tenant_id=PLATFORM,
                    account_id=OPERATOR_ACCOUNT,
                    username="info@th.com.ge",
                    email="info@th.com.ge",
                    full_name="The Operator",
                    password_hash=UNUSABLE_PASSWORD_HASH,
                    role=Role.SUPERUSER,
                    is_active=True,
                )
            )
            await session.commit()
        session.info["audited"] = audited
        # A request runs with its own tenant in context; so do these.
        with tenant_scope(PLATFORM):
            yield session
    await engine.dispose()


def _operator(**overrides: Any) -> AuthenticatedSession:
    session = AuthenticatedSession(
        session_id=7,
        user_id=OPERATOR,
        username="info@th.com.ge",
        full_name="The Operator",
        email="info@th.com.ge",
        role=Role.SUPERUSER,
        tenant_id=PLATFORM,
        tenant_slug="suliko",
        tenant_name="Suliko",
        plan=TenantPlan.BUREAU,
        onboarding_required=False,
        must_change_password=False,
        has_mfa=False,
        permissions=effective_permissions(Role.SUPERUSER, TenantPlan.BUREAU),
        mfa_satisfied_at=datetime.now(UTC),
        impersonated_by_user_id=None,
        account_id=OPERATOR_ACCOUNT,
    )
    return replace(session, **overrides)


async def _grant(
    db: AsyncSession, backend: FakeBackend, *identifiers: str
) -> list[platform.SuperuserGrantResult]:
    out = await platform.grant_superusers(
        platform.SuperuserGrant(identifiers=list(identifiers)), db, _operator(), backend
    )
    return out.results


async def _rows(db: AsyncSession) -> list[User]:
    with bypass_tenant_scope():
        return list((await db.execute(select(User).order_by(User.id))).scalars().all())


# ── Letting someone in ──────────────────────────────────────────────────────


async def test_a_suliko_account_becomes_a_superuser_who_can_sign_in(db: AsyncSession) -> None:
    backend = FakeBackend([person("g1", "nino@suliko.ge")])

    (result,) = await _grant(db, backend, "nino@suliko.ge")

    assert result.status == "granted"
    assert result.superuser is not None
    assert (result.superuser.login, result.superuser.tenant_slug) == ("nino@suliko.ge", "suliko")
    assert result.superuser.signs_in_with_suliko

    row = (await _rows(db))[-1]
    assert (row.role, row.tenant_id, row.is_active) == (Role.SUPERUSER, PLATFORM, True)
    assert not row.invitation_pending
    # The password is suliko.ge's: nothing here could be typed to sign in.
    assert row.password_hash == UNUSABLE_PASSWORD_HASH
    account = await db.get(Account, row.account_id)
    assert account is not None
    assert (account.suliko_user_id, account.password_hash) == ("g1", UNUSABLE_PASSWORD_HASH)

    # What the sign-in chooser reads: the platform organisation, as superuser,
    # carrying the platform permissions.
    (membership,) = await memberships(db, account.id)
    assert (membership.tenant.slug, membership.user.role) == ("suliko", Role.SUPERUSER)
    assert Permission.PLATFORM_TENANTS in effective_permissions(
        membership.user.role, TenantPlan.BUREAU
    )


async def test_someone_who_signs_in_with_a_phone_number_is_let_in_too(db: AsyncSession) -> None:
    backend = FakeBackend([person("g2", "599123456", "Gela", "")])

    (result,) = await _grant(db, backend, "+995 599 12 34 56")

    assert result.status == "granted"
    assert result.superuser is not None
    assert result.superuser.login == "599123456"
    row = (await _rows(db))[-1]
    assert (row.username, row.email, row.role) == ("599123456", None, Role.SUPERUSER)


async def test_each_line_gets_its_own_answer(db: AsyncSession) -> None:
    """A list is the normal use: one bad line must not stop the good ones."""
    backend = FakeBackend([person("g1", "nino@suliko.ge"), person("g2", "599123456", "Gela", "")])

    results = await _grant(
        db, backend, "nino@suliko.ge", "nobody@suliko.ge", "not an address", " 599123456 ", ""
    )

    assert [(r.identifier, r.status) for r in results] == [
        ("nino@suliko.ge", "granted"),
        ("nobody@suliko.ge", "not_found"),
        ("not an address", "invalid"),
        ("599123456", "granted"),
    ]
    assert len(await _rows(db)) == 3  # the operator and the two let in


async def test_only_a_person_suliko_knows_can_be_named(db: AsyncSession) -> None:
    """Otherwise this endpoint would mint a superuser out of any address typed
    into it — with no password, and nobody to say whose it is."""
    (result,) = await _grant(db, FakeBackend([]), "stranger@example.com")

    assert result.status == "not_found"
    assert result.superuser is None
    assert len(await _rows(db)) == 1
    with bypass_tenant_scope():
        assert len((await db.execute(select(Account))).scalars().all()) == 1


async def test_naming_someone_twice_changes_nothing(db: AsyncSession) -> None:
    backend = FakeBackend([person("g1", "nino@suliko.ge")])
    await _grant(db, backend, "nino@suliko.ge")

    (again,) = await _grant(db, backend, "NINO@suliko.ge")

    assert again.status == "already"
    assert len(await _rows(db)) == 2


async def test_a_member_of_the_organisation_is_promoted_not_doubled(db: AsyncSession) -> None:
    backend = FakeBackend([person("g1", "nino@suliko.ge")])
    account = Account(
        id=5, email="nino@suliko.ge", suliko_user_id="g1", password_hash="!", full_name="Nino"
    )
    db.add(account)
    await db.flush()
    staff = User(
        id=5,
        tenant_id=PLATFORM,
        account_id=5,
        username="nino@suliko.ge",
        email="nino@suliko.ge",
        full_name="Nino",
        password_hash=UNUSABLE_PASSWORD_HASH,
        role=Role.STAFF,
        is_active=False,
    )
    db.add(staff)
    await db.flush()
    db.add(UserPermissionOverride(user_id=5, permission="orders.write", granted=False))
    await db.flush()

    (result,) = await _grant(db, backend, "nino@suliko.ge")

    assert result.status == "granted"
    assert len(await _rows(db)) == 2
    assert (staff.role, staff.is_active) == (Role.SUPERUSER, True)
    # What was unticked for them as staff does not trim a superuser.
    assert (await db.execute(select(UserPermissionOverride))).scalars().all() == []
    # And sessions opened as staff do not carry on under the old permissions.
    assert staff.sessions_invalid_before is not None
    assert db.info["audited"][-1]["before"] == {"role": "staff"}


async def test_an_office_password_gives_way_to_the_suliko_one(db: AsyncSession) -> None:
    """Naming the console-made superuser's own address links that account:
    from then on it signs in with its suliko.ge password, and the answer says
    so, because the old password stops working on the spot."""
    backend = FakeBackend([person("g9", "info@th.com.ge", "The", "Operator")])

    (result,) = await _grant(db, backend, "info@th.com.ge")

    assert result.status == "already"
    assert result.password_moved_to_suliko
    account = await db.get(Account, OPERATOR_ACCOUNT)
    assert account is not None
    assert (account.suliko_user_id, account.password_hash) == ("g9", UNUSABLE_PASSWORD_HASH)
    assert db.info["audited"][-1]["action"] == "platform.superuser_linked_to_suliko"


async def test_a_sign_in_that_is_someone_elses_row_is_refused(db: AsyncSession) -> None:
    """Usernames are unique per organisation. A row under this address that
    belongs to another account is not handed to whoever suliko.ge says holds
    the address now."""
    other = Account(id=6, phone="555000111", password_hash="!", full_name="Somebody Else")
    db.add(other)
    await db.flush()
    db.add(
        User(
            id=6,
            tenant_id=PLATFORM,
            account_id=6,
            username="nino@suliko.ge",
            email=None,
            full_name="Somebody Else",
            password_hash=UNUSABLE_PASSWORD_HASH,
            role=Role.ADMIN,
            is_active=True,
        )
    )
    await db.flush()

    (result,) = await _grant(db, FakeBackend([person("g1", "nino@suliko.ge")]), "nino@suliko.ge")

    assert result.status == "conflict"
    assert [r.role for r in await _rows(db)] == [Role.SUPERUSER, Role.ADMIN]


async def test_the_membership_lands_in_the_operators_own_organisation(db: AsyncSession) -> None:
    """Never in a bureau: the request names no tenant at all, and a superuser
    row inside a customer's organisation would show up on their Users screen."""
    assert "tenant" not in platform.SuperuserGrant.model_fields

    await _grant(db, FakeBackend([person("g1", "nino@suliko.ge")]), "nino@suliko.ge")

    assert {r.tenant_id for r in await _rows(db)} == {PLATFORM}


async def test_suliko_being_down_is_an_error_not_a_not_found(db: AsyncSession) -> None:
    """ "Nobody has that sign-in" would be a guess, and the caller would go
    and tell the person to register again."""
    backend = FakeBackend([person("g1", "nino@suliko.ge")], directory_down=True)

    with pytest.raises(SulikoUnavailableError):
        await _grant(db, backend, "nino@suliko.ge")


async def test_without_suliko_there_is_nobody_to_let_in(db: AsyncSession) -> None:
    with pytest.raises(ConflictError, match="not connected to suliko"):
        await _grant(db, FakeBackend(enabled=False), "nino@suliko.ge")


async def test_a_grant_is_audited_with_who_and_where(db: AsyncSession) -> None:
    await _grant(db, FakeBackend([person("g1", "nino@suliko.ge")]), "nino@suliko.ge")

    (entry,) = db.info["audited"]
    assert entry["action"] == "platform.superuser_granted"
    assert entry["tenant_id"] == PLATFORM
    assert entry["after"]["suliko_user_id"] == "g1"


# ── The list ────────────────────────────────────────────────────────────────


async def test_the_list_shows_every_superuser_and_how_they_sign_in(db: AsyncSession) -> None:
    await _grant(db, FakeBackend([person("g1", "nino@suliko.ge")]), "nino@suliko.ge")

    listed = await platform.list_superusers(db, _operator())

    assert [(s.login, s.signs_in_with_suliko) for s in listed] == [
        ("info@th.com.ge", False),
        ("nino@suliko.ge", True),
    ]


# ── Taking it away ──────────────────────────────────────────────────────────


async def test_removing_a_superuser_removes_the_membership_only(db: AsyncSession) -> None:
    (granted,) = await _grant(db, FakeBackend([person("g1", "nino@suliko.ge")]), "nino@suliko.ge")
    assert granted.superuser is not None

    await platform.revoke_superuser(granted.superuser.id, db, _operator())
    await db.flush()

    assert [r.id for r in await _rows(db)] == [OPERATOR]
    # Their account is theirs: it stays, with whatever else it belongs to.
    with bypass_tenant_scope():
        assert len((await db.execute(select(Account))).scalars().all()) == 2
    assert db.info["audited"][-1]["action"] == "platform.superuser_revoked"


async def test_nobody_removes_themselves(db: AsyncSession) -> None:
    """Which is also why one superuser is always left."""
    with pytest.raises(ValidationError, match="your own platform access"):
        await platform.revoke_superuser(OPERATOR, db, _operator())


async def test_only_a_superuser_row_can_be_removed_here(db: AsyncSession) -> None:
    """Or this would be a way to delete anybody, in any bureau, by id."""
    with bypass_tenant_scope():
        db.add(
            User(
                id=9,
                tenant_id=ACME,
                username="owner@acme.ge",
                email="owner@acme.ge",
                full_name="Acme Owner",
                password_hash=UNUSABLE_PASSWORD_HASH,
                role=Role.OWNER,
                is_active=True,
            )
        )
        await db.flush()

    with pytest.raises(NotFoundError):
        await platform.revoke_superuser(9, db, _operator())
    with pytest.raises(NotFoundError):
        await platform.revoke_superuser(404, db, _operator())
