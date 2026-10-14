"""A translator's language pairs run both ways.

``both_ways`` is the rule, tested on its own; the rest shows it at work through
the translators handlers (SQLite, as ``test_translator_profile.py`` does).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from suliko.api.v1 import translators as api
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain.plans import TenantPlan
from suliko.domain.translator_pairs import Pair, both_ways
from suliko.models.directory import Translator, TranslatorLanguagePair
from suliko.models.reference import Language
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role
from suliko.security.permissions import permissions_for_role
from suliko.security.sessions import AuthenticatedSession

ACME = 1
MODELS = (Tenant, Translator, TranslatorLanguagePair, Language)

install_tenant_filter()


# ── The rule ────────────────────────────────────────────────────────────────


def test_each_pair_is_followed_by_its_reverse() -> None:
    stored = both_ways([Pair("ka", "en", Decimal("20")), Pair("ka", "de")])

    assert [(p.source, p.target) for p in stored] == [
        ("ka", "en"),
        ("en", "ka"),
        ("ka", "de"),
        ("de", "ka"),
    ]


def test_the_reverse_has_no_rate_of_its_own() -> None:
    """Empty means "the one that is set": the screens show the forward rate."""
    forward, reverse = both_ways([Pair("ka", "en", Decimal("20"))])

    assert forward.rate == Decimal("20")
    assert reverse.rate is None


def test_a_one_way_pair_gets_no_reverse() -> None:
    stored = both_ways([Pair("ka", "en", one_way=True), Pair("ka", "de")])

    assert [(p.source, p.target) for p in stored] == [("ka", "en"), ("ka", "de"), ("de", "ka")]
    assert not any(p.one_way for p in stored)


def test_a_reverse_the_caller_listed_keeps_its_own_rate() -> None:
    stored = both_ways([Pair("ka", "en", Decimal("20")), Pair("en", "ka", Decimal("25"))])

    assert [(p.source, p.target, p.rate) for p in stored] == [
        ("ka", "en", Decimal("20")),
        ("en", "ka", Decimal("25")),
    ]


def test_nothing_is_added_twice() -> None:
    """Listing a pair and its reverse yields those two rows, not four."""
    stored = both_ways([Pair("ka", "en"), Pair("en", "ka")])

    assert [(p.source, p.target) for p in stored] == [("ka", "en"), ("en", "ka")]


def test_no_pairs_stay_no_pairs() -> None:
    assert both_ways([]) == []


# ── Through the handlers ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def record(_db: object, _session: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("suliko.core.audit.record", record)
    return calls


def _session() -> AuthenticatedSession:
    return AuthenticatedSession(
        session_id=1,
        user_id=1,
        username="manager",
        full_name="Bureau Manager",
        email="manager@acme.test",
        role=Role.MANAGER,
        tenant_id=ACME,
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
                sync, tables=[Base.metadata.tables[m.__tablename__] for m in MODELS]
            )
        )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with sessionmaker() as db:
        with bypass_tenant_scope():
            db.add(
                Tenant(
                    id=ACME,
                    slug="acme",
                    display_name="Acme",
                    status=TenantStatus.ACTIVE,
                    locale="ka",
                )
            )
            await db.flush()
            for code in ("ka", "en", "de"):
                db.add(Language(tenant_id=ACME, code=code, name_en=code, name_ka=code))
        await db.commit()
    yield sessionmaker
    await engine.dispose()


def pair(
    source: str, target: str, rate: str | None = None, *, one_way: bool = False
) -> api.LanguagePairIn:
    return api.LanguagePairIn(
        source_language=source,
        target_language=target,
        rate_per_page=Decimal(rate) if rate is not None else None,
        one_way=one_way,
    )


async def create(maker: async_sessionmaker[AsyncSession], **fields: object) -> api.TranslatorDetail:
    with tenant_scope(ACME):
        async with maker() as db:
            detail = await api.create_translator(
                api.TranslatorCreate(**{"name": "Nino", **fields}),  # type: ignore[arg-type]
                db=db,
                session=_session(),
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
                session=_session(),
                _=None,
            )
            await db.commit()
            return detail


def stored(detail: api.TranslatorSummary) -> list[tuple[str, str, Decimal | None]]:
    return [(p.source_language, p.target_language, p.rate_per_page) for p in detail.language_pairs]


async def test_a_new_pair_is_saved_both_ways(maker: async_sessionmaker[AsyncSession]) -> None:
    detail = await create(maker, language_pairs=[pair("ka", "en", "20")])

    assert stored(detail) == [("ka", "en", Decimal("20")), ("en", "ka", None)]


async def test_a_one_way_pair_stays_one_way(maker: async_sessionmaker[AsyncSession]) -> None:
    detail = await create(maker, language_pairs=[pair("ka", "en", one_way=True)])

    assert stored(detail) == [("ka", "en", None)]


async def test_a_pair_and_its_own_reverse_rate_are_both_kept(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    detail = await create(maker, language_pairs=[pair("ka", "en", "20"), pair("en", "ka", "25")])

    assert stored(detail) == [("ka", "en", Decimal("20")), ("en", "ka", Decimal("25"))]


async def test_editing_the_pairs_writes_both_ways_again(
    maker: async_sessionmaker[AsyncSession], audit: list[dict[str, Any]]
) -> None:
    created = await create(maker, language_pairs=[pair("ka", "en")])

    updated = await update(maker, created.id, language_pairs=[pair("de", "ka", "30")])

    assert stored(updated) == [("de", "ka", Decimal("30")), ("ka", "de", None)]
    # The audit log records what was stored, not only what was sent.
    entry = next(e for e in audit if e["action"] == "translator.updated")
    assert entry["after"]["language_pairs"] == [
        {"source": "de", "target": "ka"},
        {"source": "ka", "target": "de"},
    ]


async def test_the_same_direction_twice_is_still_refused() -> None:
    with pytest.raises(ValueError, match="twice"):
        api.TranslatorCreate(name="Nino", language_pairs=[pair("ka", "en"), pair("ka", "en")])
