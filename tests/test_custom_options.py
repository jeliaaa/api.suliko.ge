"""A bureau's own dropdown values, and statuses that may be picked.

In-memory SQLite, like ``test_password_reset.py``: ``custom_options`` is plain
ORM with no Postgres-only column. The endpoint functions are called directly
with a hand-built session, which is what FastAPI would inject.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from suliko.api.v1.options import (
    LIST_PERMISSIONS,
    OptionIn,
    add_option,
    delete_option,
    list_options,
)
from suliko.api.v1.orders import _resolve_status
from suliko.core.errors import PermissionDeniedError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.plans import TenantPlan
from suliko.domain.statuses import SELECTABLE_STATUSES, STATUS_DEFINITIONS
from suliko.models.reference import CustomOption, OptionList
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession

TENANT = 1
OTHER = 2


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[Tenant.__table__, CustomOption.__table__])
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        with bypass_tenant_scope():
            for tenant_id, slug in ((TENANT, "acme"), (OTHER, "other")):
                session.add(
                    Tenant(
                        id=tenant_id,
                        slug=slug,
                        display_name=slug.title(),
                        status=TenantStatus.ACTIVE,
                        locale="ka",
                    )
                )
            session.add(
                CustomOption(tenant_id=OTHER, list_key=OptionList.ORDER_STATUS, value="Theirs")
            )
            await session.commit()
        with tenant_scope(TENANT):
            yield session
    await engine.dispose()


def _session(*permissions: Permission) -> AuthenticatedSession:
    return AuthenticatedSession(
        session_id=1,
        user_id=1,
        username="nino",
        full_name="Nino",
        email="nino@acme.ge",
        role=Role.STAFF,
        tenant_id=TENANT,
        tenant_slug="acme",
        tenant_name="Acme",
        plan=TenantPlan.BUREAU,
        onboarding_required=False,
        must_change_password=False,
        has_mfa=False,
        permissions=frozenset(permissions),
        mfa_satisfied_at=None,
        impersonated_by_user_id=None,
    )


# ── Which statuses can be picked ────────────────────────────────────────────


def test_the_six_pickable_statuses_are_all_known() -> None:
    assert len(SELECTABLE_STATUSES) == 6
    assert set(SELECTABLE_STATUSES) <= set(STATUS_DEFINITIONS)
    assert STATUS_DEFINITIONS["completed"].label == "Delivered"


async def test_a_built_in_status_is_stored_normalised(db: AsyncSession) -> None:
    assert await _resolve_status(db, "  Being Notarised ") == "being notarised"


async def test_a_retired_status_is_refused(db: AsyncSession) -> None:
    with pytest.raises(ValidationError):
        await _resolve_status(db, "payed")


async def test_a_custom_status_is_stored_as_the_bureau_spelled_it(db: AsyncSession) -> None:
    await add_option(
        OptionList.ORDER_STATUS,
        OptionIn(value="In Review"),
        db,
        _session(Permission.SETTINGS_MANAGE),
    )
    assert await _resolve_status(db, "in review") == "In Review"


async def test_another_bureaus_status_is_not_ours(db: AsyncSession) -> None:
    with pytest.raises(ValidationError):
        await _resolve_status(db, "Theirs")


# ── Adding, listing, removing ───────────────────────────────────────────────


def test_every_list_has_permissions() -> None:
    assert set(LIST_PERMISSIONS) == set(OptionList)


def test_a_value_is_trimmed_and_must_not_be_blank() -> None:
    assert OptionIn(value="  Facebook   ads ").value == "Facebook ads"
    with pytest.raises(PydanticValidationError):
        OptionIn(value="   ")


async def test_anyone_who_records_clients_can_add_a_source(db: AsyncSession) -> None:
    added = await add_option(
        OptionList.ACQUISITION_SOURCE,
        OptionIn(value="Facebook"),
        db,
        _session(Permission.CLIENTS_WRITE),
    )
    listed = await list_options(
        OptionList.ACQUISITION_SOURCE, db, _session(Permission.CLIENTS_READ)
    )
    assert [o.value for o in listed] == ["Facebook"]
    assert listed[0].id == added.id


async def test_adding_the_same_value_again_returns_it(db: AsyncSession) -> None:
    session = _session(Permission.CLIENTS_WRITE)
    first = await add_option(OptionList.ACQUISITION_SOURCE, OptionIn(value="Google"), db, session)
    again = await add_option(OptionList.ACQUISITION_SOURCE, OptionIn(value="google"), db, session)
    assert again.id == first.id
    rows = (await db.execute(select(CustomOption))).scalars().all()
    assert len(rows) == 1


async def test_statuses_are_a_settings_decision(db: AsyncSession) -> None:
    with pytest.raises(PermissionDeniedError):
        await add_option(
            OptionList.ORDER_STATUS,
            OptionIn(value="Waiting"),
            db,
            _session(Permission.CLIENTS_WRITE),
        )


async def test_a_custom_status_cannot_shadow_a_built_in(db: AsyncSession) -> None:
    session = _session(Permission.SETTINGS_MANAGE)
    for clash in ("new", "Delivered", "Sent to Translator"):
        with pytest.raises(ValidationError):
            await add_option(OptionList.ORDER_STATUS, OptionIn(value=clash), db, session)


async def test_removing_needs_settings(db: AsyncSession) -> None:
    added = await add_option(
        OptionList.ACQUISITION_SOURCE,
        OptionIn(value="Radio"),
        db,
        _session(Permission.CLIENTS_WRITE),
    )
    with pytest.raises(PermissionDeniedError):
        await delete_option(
            OptionList.ACQUISITION_SOURCE, added.id, db, _session(Permission.CLIENTS_WRITE)
        )
    await delete_option(
        OptionList.ACQUISITION_SOURCE, added.id, db, _session(Permission.SETTINGS_MANAGE)
    )
    assert (
        await list_options(OptionList.ACQUISITION_SOURCE, db, _session(Permission.CLIENTS_READ))
        == []
    )
