"""A translator's profile: the language pairs they work in, and what they can do.

The pairs live in ``translator_language_pairs`` and are written as a whole; the
flags say whether they can certify at a notary and whether they translate
documents, interpret, or both. Run against SQLite through the handlers
themselves, as ``test_portal_api.py`` does for the staff side.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pydantic
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from suliko.api.v1 import translators as api
from suliko.core.errors import NotFoundError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.plans import TenantPlan
from suliko.models.directory import Translator, TranslatorLanguagePair
from suliko.models.reference import Language
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role
from suliko.security.permissions import permissions_for_role
from suliko.security.sessions import AuthenticatedSession

ACME, GLOBEX = 1, 2
MODELS = (Tenant, Translator, TranslatorLanguagePair, Language)

install_tenant_filter()


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """audit_log uses JSONB and INET, which SQLite cannot create: record the
    calls instead, as test_portal_api.py does."""
    calls: list[dict[str, Any]] = []

    async def record(_db: object, _session: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("suliko.core.audit.record", record)
    return calls


def _session(tenant_id: int) -> AuthenticatedSession:
    return AuthenticatedSession(
        session_id=1,
        user_id=1,
        username="manager",
        full_name="Bureau Manager",
        email="manager@acme.test",
        role=Role.MANAGER,
        tenant_id=tenant_id,
        tenant_slug="acme",
        tenant_name="Acme Translations",
        plan=TenantPlan.BUREAU,
        onboarding_required=False,
        must_change_password=False,
        has_mfa=False,
        permissions=permissions_for_role(Role.MANAGER),
        mfa_satisfied_at=None,
        impersonated_by_user_id=None,
    )


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[Base.metadata.tables[m.__tablename__] for m in MODELS],
            )
        )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with sessionmaker() as db:
        with bypass_tenant_scope():
            for tenant_id, slug in ((ACME, "acme"), (GLOBEX, "globex")):
                db.add(
                    Tenant(
                        id=tenant_id,
                        slug=slug,
                        display_name=slug.title(),
                        status=TenantStatus.ACTIVE,
                        locale="ka",
                    )
                )
            await db.flush()
            for tenant_id, codes in ((ACME, ("ka", "en", "de")), (GLOBEX, ("ka", "fr"))):
                for code in codes:
                    db.add(Language(tenant_id=tenant_id, code=code, name_en=code, name_ka=code))
        await db.commit()
    yield sessionmaker
    await engine.dispose()


def pair(source: str, target: str, rate: str | None = None) -> api.LanguagePairIn:
    return api.LanguagePairIn(
        source_language=source,
        target_language=target,
        rate_per_page=Decimal(rate) if rate is not None else None,
    )


async def create(
    maker: async_sessionmaker[AsyncSession], tenant_id: int = ACME, **fields: object
) -> api.TranslatorDetail:
    with tenant_scope(tenant_id):
        async with maker() as db:
            detail = await api.create_translator(
                api.TranslatorCreate(**{"name": "Nino", **fields}),  # type: ignore[arg-type]
                db=db,
                session=_session(tenant_id),
                _=None,
            )
            await db.commit()
            return detail


async def update(
    maker: async_sessionmaker[AsyncSession], translator_id: int, **fields: object
) -> api.TranslatorDetail:
    with tenant_scope(ACME):
        async with maker() as db:
            detail = await api.update_translator(
                translator_id,
                api.TranslatorUpdate(**fields),  # type: ignore[arg-type]
                db=db,
                session=_session(ACME),
                _=None,
            )
            await db.commit()
            return detail


def pairs_of(detail: api.TranslatorSummary) -> list[tuple[str, str]]:
    return [(p.source_language, p.target_language) for p in detail.language_pairs]


# ── Creating ────────────────────────────────────────────────────────────────


async def test_a_new_translator_keeps_its_pairs_and_flags(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    detail = await create(
        maker,
        language_pairs=[pair("ka", "en", "20"), pair("EN", "ka")],
        can_notarize=True,
        works_oral=True,
    )

    assert pairs_of(detail) == [("ka", "en"), ("en", "ka")]
    assert detail.language_pairs[0].rate_per_page == Decimal("20")
    assert detail.language_pairs[1].rate_per_page is None
    assert detail.can_notarize and detail.works_written and detail.works_oral


async def test_by_default_a_translator_does_documents_only(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """What every translator was before the flags existed."""
    detail = await create(maker)

    assert detail.works_written and not detail.works_oral and not detail.can_notarize
    assert detail.language_pairs == []


async def test_a_pair_must_use_the_bureaus_own_languages(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """French is Globex's language, not Acme's."""
    with pytest.raises(ValidationError, match="fr"):
        await create(maker, language_pairs=[pair("ka", "fr")])


def test_a_pair_needs_two_different_languages() -> None:
    with pytest.raises(pydantic.ValidationError):
        pair("ka", "KA")


def test_a_pair_cannot_be_listed_twice() -> None:
    with pytest.raises(pydantic.ValidationError, match="twice"):
        api.TranslatorCreate(name="Nino", language_pairs=[pair("ka", "en"), pair("ka", "en")])


def test_a_translator_does_some_kind_of_work() -> None:
    with pytest.raises(pydantic.ValidationError, match="written work, oral work"):
        api.TranslatorCreate(name="Nino", works_written=False, works_oral=False)


# ── Changing ────────────────────────────────────────────────────────────────


async def test_sending_pairs_replaces_the_whole_set(
    maker: async_sessionmaker[AsyncSession], audit: list[dict[str, Any]]
) -> None:
    created = await create(maker, language_pairs=[pair("ka", "en"), pair("en", "ka")])

    updated = await update(maker, created.id, language_pairs=[pair("de", "ka", "30")])

    assert pairs_of(updated) == [("de", "ka")]
    assert updated.language_pairs[0].rate_per_page == Decimal("30")
    # The audit log says what the set was and what it became.
    entry = next(e for e in audit if e["action"] == "translator.updated")
    assert entry["before"]["language_pairs"] == [
        {"source": "ka", "target": "en"},
        {"source": "en", "target": "ka"},
    ]
    assert entry["after"]["language_pairs"] == [{"source": "de", "target": "ka"}]


async def test_leaving_pairs_out_keeps_them(maker: async_sessionmaker[AsyncSession]) -> None:
    created = await create(maker, language_pairs=[pair("ka", "en")])

    updated = await update(maker, created.id, phone="555 12 34 56", can_notarize=True)

    assert pairs_of(updated) == [("ka", "en")]
    assert updated.can_notarize


async def test_an_empty_list_removes_every_pair(maker: async_sessionmaker[AsyncSession]) -> None:
    created = await create(maker, language_pairs=[pair("ka", "en")])

    updated = await update(maker, created.id, language_pairs=[])

    assert updated.language_pairs == []


async def test_switching_off_the_only_kind_of_work_is_refused(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    created = await create(maker)

    with pytest.raises(ValidationError, match="written work, oral work"):
        await update(maker, created.id, works_written=False)

    # Swapping one for the other is fine.
    updated = await update(maker, created.id, works_written=False, works_oral=True)
    assert updated.works_oral and not updated.works_written


# ── Reading and removing ────────────────────────────────────────────────────


async def test_the_list_shows_each_translators_own_pairs(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    nino = await create(maker, name="Nino", language_pairs=[pair("ka", "en")])
    levan = await create(maker, name="Levan", language_pairs=[pair("de", "ka"), pair("ka", "de")])
    await create(maker, GLOBEX, name="Globex person", language_pairs=[pair("ka", "fr")])

    with tenant_scope(ACME):
        async with maker() as db:
            page = await api.list_translators(db=db, _=None, sort="name")

    by_id = {item.id: pairs_of(item) for item in page.items}
    assert by_id == {nino.id: [("ka", "en")], levan.id: [("de", "ka"), ("ka", "de")]}


async def test_removing_a_translator_removes_its_pairs(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The pairs have no foreign key to cascade from."""
    created = await create(maker, language_pairs=[pair("ka", "en"), pair("en", "ka")])

    with tenant_scope(ACME):
        async with maker() as db:
            await api.delete_translator(created.id, db=db, session=_session(ACME), _=None)
            await db.commit()

    async with maker() as db:
        with bypass_tenant_scope():
            left = (await db.execute(select(TranslatorLanguagePair))).scalars().all()
    assert left == []

    with tenant_scope(ACME), pytest.raises(NotFoundError):
        async with maker() as db:
            await api.get_translator(created.id, db=db, _=None)
