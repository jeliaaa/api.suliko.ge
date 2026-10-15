"""Importing one file that holds several kinds of record.

The planner and the judge (``domain/imports_combined.py``) are pure and tested on
the two invented ``.xls`` samples in ``tests/fixtures/import-samples``: a workbook
with a sheet per kind, and one flat sheet with everything on every row. The
endpoints run for real against in-memory SQLite, like ``test_imports.py``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from suliko.api.v1 import imports as api
from suliko.core.errors import ConflictError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain import imports as engine
from suliko.domain import imports_combined as combined
from suliko.domain.imports_orders import OrdersContext
from suliko.models.directory import Client, Notary, Translator, TranslatorLanguagePair
from suliko.models.imports import ImportBatch
from suliko.models.order import Order, OrderDocument
from suliko.models.reference import DocumentType, Language
from suliko.models.tenant import Tenant, TenantStatus
from test_imports import ACME, MODELS, Upload, csv_bytes
from test_imports import _session as session_of

install_tenant_filter()

SAMPLES = Path(__file__).parent / "fixtures" / "import-samples"
SAMPLE_1 = "sample-1-workbook-by-sheets.xls"
SAMPLE_2 = "sample-2-one-flat-sheet.xls"

LANGUAGES = (
    ("ka", "Georgian", "ქართული"),
    ("en", "English", "ინგლისური"),
    ("ru", "Russian", "რუსული"),
    ("de", "German", "გერმანული"),
    ("fr", "French", "ფრანგული"),
)


def sample(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def lookup(word: str) -> str | None:
    for code, name_en, name_ka in LANGUAGES:
        if word.strip().casefold() in (code, name_en.casefold(), name_ka):
            return code
    return None


def sheets_of(name: str) -> list[engine.RawSheet]:
    return engine.read_workbook(sample(name), name)


def plans_of(
    name: str, choices: dict[int, combined.Choice] | None = None
) -> list[combined.SheetPlan]:
    return combined.plan_sheets(sheets_of(name), choices)


def orders_ctx() -> OrdersContext:
    return OrdersContext(
        language_lookup=lookup,
        document_types={"passport": (1, "Passport"), "diploma": (2, "Diploma")},
        default_document_type=(1, "Passport"),
        status_lookup=lambda text: {"delivered": "completed", "cancelled": "cancelled"}.get(
            text.casefold()
        ),
    )


def judge(name: str, directory: combined.Directory | None = None) -> combined.WorkbookJudged:
    return combined.judge_workbook(
        plans_of(name), directory or combined.Directory(), orders_ctx(), lookup
    )


def by_name(judged: combined.WorkbookJudged) -> dict[str, combined.SheetResult]:
    return {s.plan.name: s for s in judged.sheets}


def counts(sheet: combined.SheetResult) -> tuple[int, int, int]:
    return sheet.count("ok"), sheet.count("duplicate"), sheet.count("error")


# ── Reading and recognising ─────────────────────────────────────────────────


def test_an_old_xls_workbook_is_read_sheet_by_sheet() -> None:
    sheets = sheets_of(SAMPLE_1)
    assert [s.name for s in sheets] == [
        "შეკვეთები 2026",
        "კლიენტები",
        "თარჯიმნები",
        "ნოტარიუსები",
        "ინფო",
    ]
    # Rows are numbered as the spreadsheet numbers them, header included.
    assert sheets[1].rows[0][0] == 1
    assert len(sheets_of(SAMPLE_2)) == 1


def test_each_sheet_of_a_workbook_is_recognised_by_its_columns() -> None:
    plans = plans_of(SAMPLE_1)
    assert [(p.kind, p.confidence) for p in plans] == [
        ("orders", "high"),
        ("clients", "high"),
        ("translators", "high"),
        ("notaries", "high"),
        (None, "none"),
    ]
    assert [p.header_row for p in plans[:4]] == [1, 1, 1, 1]
    # The notaries' own columns are what say so, not a column called "notary".
    assert "registration_number" in plans[3].mapping
    assert "can_notarize" in plans[2].mapping


def test_a_cover_sheet_is_left_out_but_can_be_chosen_by_hand() -> None:
    cover = plans_of(SAMPLE_1)[4]
    assert (cover.kind, cover.why) == (None, "nothing")
    # Its columns were unknown, so the first row with two cells is still its header.
    assert cover.header_row == 3
    chosen = plans_of(
        SAMPLE_1, {4: combined.Choice(kind="clients", mapping={"name": 0, "phone": 1})}
    )[4]
    assert chosen.kind == "clients"
    assert chosen.why == "chosen"
    assert chosen.mapping == {"name": 0, "phone": 1}


def test_the_header_is_found_below_a_title_and_the_rows_that_are_not_records_are_set_aside() -> (
    None
):
    (plan,) = plans_of(SAMPLE_2)
    assert (plan.kind, plan.header_row) == ("orders", 3)
    assert len(plan.rows) == 20
    assert plan.skipped == [(4, "heading"), (14, "heading"), (27, "totals"), (29, "heading")]


def test_a_phone_column_belongs_to_the_person_named_to_its_left() -> None:
    (plan,) = plans_of(SAMPLE_2)
    # ტელეფონი is the header of columns D, N and Q.
    assert plan.mapping["client_phone"] == 3
    assert plan.mapping["translator_phone"] == 13
    assert plan.mapping["notary_phone"] == 16
    assert plan.mapping["notary"] == 15
    assert plan.mapping["notary_cost"] == 17
    # "გვ." is an abbreviation of pages; the № counter is not a field.
    assert plan.mapping["pages"] == 9
    assert 0 not in plan.mapping.values()


def test_a_person_with_only_a_name_stays_but_a_stray_cell_in_the_counter_column_goes() -> None:
    mapping = {"name": 1, "phone": 2}
    rows: list[tuple[int, list[Any]]] = [
        (2, [1, "Nino", "555111222"]),
        (3, [2, "Gia", None]),
        (4, ["March", None, None]),
        (5, [None, None, "599000111"]),
        (6, [None, "Total", None]),
    ]
    kept, skipped = combined.split_rows("clients", mapping, rows)
    # A phone with no name is a bad row for the judge to say so, not a heading.
    assert [n for n, _ in kept] == [2, 3, 5]
    assert skipped == [(4, "heading"), (6, "totals")]
    # In an orders sheet no single cell can be an order.
    kept, skipped = combined.split_rows("orders", {"client_name": 1, "order_date": 0}, rows)
    assert [n for n, _ in kept] == [2, 3]
    assert [n for n, _ in skipped] == [4, 5, 6]


def test_a_sheet_with_only_a_name_and_a_phone_is_a_guess_the_person_should_check() -> None:
    kind, confidence, why, _ = combined.classify("Sheet1", ["Name", "Phone"])
    assert (kind, confidence, why) == ("clients", "low", "unsure")


def test_the_sheet_name_breaks_a_tie_between_kinds() -> None:
    kind, _, why, _ = combined.classify("Translators", ["Name", "Phone", "Email"])
    assert (kind, why) == ("translators", "name")


def test_the_person_can_leave_a_sheet_out_or_say_what_it_is() -> None:
    plans = plans_of(SAMPLE_1, {0: combined.Choice(skip=True), 2: combined.Choice(kind="notaries")})
    assert (plans[0].kind, plans[0].why, plans[0].rows) == (None, "chosen", plans[0].rows)
    assert plans[0].skipped == []
    assert (plans[2].kind, plans[2].why) == ("notaries", "chosen")


def test_a_header_row_that_is_not_in_the_sheet_is_refused() -> None:
    with pytest.raises(ValidationError, match="row 40"):
        plans_of(SAMPLE_1, {0: combined.Choice(header_row=40)})


def test_the_header_is_the_row_that_names_the_most_fields_not_just_the_first_wide_row() -> None:
    sheet = engine.RawSheet(
        "Clients",
        [
            (1, ["Bureau", "Acme"]),
            (2, ["Printed", "2026-01-05"]),
            (3, ["Name", "Phone", "Email", "ID"]),
            (4, ["Nino", "555111222", "nino@example.com", "01001000011"]),
        ],
    )
    (plan,) = combined.plan_sheets([sheet])
    assert (plan.header_row, plan.kind) == (3, "clients")
    assert [n for n, _ in plan.rows] == [4]


def test_a_csv_is_one_sheet_named_after_the_file() -> None:
    (sheet,) = engine.read_workbook(csv_bytes("Name,Phone\nNino,555111222\n"), "my clients.csv")
    assert sheet.name == "my clients"


# ── Judging the whole file ──────────────────────────────────────────────────


def test_a_workbook_is_judged_kind_by_kind_with_the_counts_the_samples_promise() -> None:
    judged = judge(SAMPLE_1)
    sheets = by_name(judged)
    assert counts(sheets["კლიენტები"]) == (10, 1, 1)
    assert counts(sheets["თარჯიმნები"]) == (7, 1, 0)
    assert counts(sheets["ნოტარიუსები"]) == (5, 1, 0)
    # The orders sheet comes first in the file and is judged last.
    assert counts(sheets["შეკვეთები 2026"]) == (13, 1, 1)
    assert [s.plan.kind for s in judged.sheets] == ["orders", "clients", "translators", "notaries"]


def test_an_order_finds_the_translator_the_translators_sheet_creates_and_not_one_it_lacks() -> None:
    judged = judge(SAMPLE_1)
    orders = by_name(judged)["შეკვეთები 2026"].results
    first = next(r for r in orders if r.row == 2)
    assert str(first.values["translator_id"]).startswith("newt:")
    unknown = next(r for r in orders if r.row == 12)
    assert unknown.values["translator_id"] is None
    assert any("კახა გელაძე" in w for w in unknown.warnings)
    # The translators sheet lists the translators, so the orders do not create any.
    assert judged.ctx.new_translators == {}
    assert judged.ctx.new_notaries == {}


def test_a_client_only_the_orders_know_is_created_once() -> None:
    judged = judge(SAMPLE_1)
    orders = by_name(judged)["შეკვეთები 2026"].results
    new = [r.values["client"] for r in orders if r.status == "ok" and r.values["client"]["new"]]
    assert [c["new"]["name"] for c in new] == ["ზურაბ ჩხეიძე"]
    both = [r for r in orders if r.name == "ზურაბ ჩხეიძე"]
    assert len(both) == 2
    assert both[0].values["client"]["ref"] == both[1].values["client"]["ref"]
    # None of the sheet's own clients share a ref with it.
    refs = {r.ref for r in by_name(judged)["კლიენტები"].results if r.ref}
    assert both[0].values["client"]["ref"] not in refs


def test_with_no_sheet_of_translators_or_notaries_the_orders_introduce_them() -> None:
    judged = judge(SAMPLE_2)
    (sheet,) = judged.sheets
    assert counts(sheet) == (18, 1, 1)
    assert [v["name"] for v in judged.ctx.new_translators.values()] == [
        "ეკა წულაძე",
        "ზაზა მაისურაძე",
        "ხათუნა ჯანელიძე",
    ]
    assert [v["name"] for v in judged.ctx.new_notaries.values()] == [
        "თეონა გოგოლაძე",
        "ნესტან გაბუნია",
        "მიხეილ ახალაია",
    ]
    clients = {
        r.values["client"]["ref"]
        for r in sheet.results
        if r.status == "ok" and r.values["client"]["new"]
    }
    assert len(clients) == 10
    # The first time a translator is named they come with a phone; later rows only repeat the name.
    eka = next(v for v in judged.ctx.new_translators.values() if v["name"] == "ეკა წულაძე")
    assert eka["phone"] == "555 12 34 56"
    assert ("ka", "en") in eka["pairs"]


def test_a_row_for_a_bad_price_or_the_same_order_is_judged_not_dropped() -> None:
    (sheet,) = judge(SAMPLE_2).sheets
    by_row = {r.row: r for r in sheet.results}
    assert by_row[20].status == "duplicate"
    assert by_row[20].matched == "row 11"
    assert by_row[23].status == "error"
    assert "შეთანხმებით" in by_row[23].messages[0]
    # A row with no client name finds its client by phone.
    assert by_row[16].status == "ok"
    assert by_row[16].values["client"]["new"] is None


def test_a_duplicate_translator_under_another_spelling_still_resolves_for_the_orders() -> None:
    translators = combined.SheetPlan(
        index=0,
        name="Translators",
        header_row=1,
        headers=["Name", "Phone", "Rate"],
        kind="translators",
        confidence="high",
        why="columns",
        mapping={"name": 0, "phone": 1, "default_rate": 2},
        rows=[
            (2, ["Eka Tsuladze", "555 12 34 56", "12"]),
            (3, ["Ekaterine Tsuladze", "555123456", "12"]),
        ],
    )
    orders = combined.SheetPlan(
        index=1,
        name="Orders",
        header_row=1,
        headers=["Date", "Client", "Languages", "Price", "Translator"],
        kind="orders",
        confidence="high",
        why="columns",
        mapping={
            "order_date": 0,
            "client_name": 1,
            "language_pair": 2,
            "price": 3,
            "translator": 4,
        },
        rows=[(2, ["01.02.2026", "Nino", "ka-en", "40", "Ekaterine Tsuladze"])],
    )
    judged = combined.judge_workbook(
        [orders, translators], combined.Directory(), orders_ctx(), lookup
    )
    sheets = by_name(judged)
    assert counts(sheets["Translators"]) == (1, 1, 0)
    (order,) = sheets["Orders"].results
    assert order.values["translator_id"] == sheets["Translators"].results[0].ref


def test_a_file_with_nothing_recognised_says_so() -> None:
    plans = [combined.Choice(skip=True)]
    with pytest.raises(ValidationError, match="Choose what each sheet holds"):
        combined.judge_workbook(
            combined.plan_sheets(sheets_of(SAMPLE_2), {0: plans[0]}),
            combined.Directory(),
            orders_ctx(),
            lookup,
        )


def test_a_missing_required_column_names_the_sheet() -> None:
    chosen = {1: combined.Choice(kind="clients", mapping={"phone": 2})}
    with pytest.raises(ValidationError, match=r"კლიენტები.*name"):
        combined.judge_workbook(
            combined.plan_sheets(sheets_of(SAMPLE_1), chosen),
            combined.Directory(),
            orders_ctx(),
            lookup,
        )


# ── The endpoints ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def record(_db: object, _session: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("suliko.core.audit.record", record)
    return calls


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine_ = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine_.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync, tables=[Base.metadata.tables[m.__tablename__] for m in MODELS]
            )
        )
    maker = async_sessionmaker(engine_, expire_on_commit=False, autoflush=False)
    async with maker() as session:
        with bypass_tenant_scope():
            session.add(
                Tenant(
                    id=ACME,
                    slug="acme",
                    display_name="Acme",
                    status=TenantStatus.ACTIVE,
                    locale="ka",
                )
            )
            await session.flush()
            for code, en, ka in LANGUAGES:
                session.add(Language(tenant_id=ACME, code=code, name_en=en, name_ka=ka))
            session.add(DocumentType(tenant_id=ACME, name_en="Passport", name_ka="პასპორტი"))
            session.add(DocumentType(tenant_id=ACME, name_en="Diploma", name_ka="დიპლომა"))
            await session.commit()
        with tenant_scope(ACME):
            yield session
    await engine_.dispose()


def upload(name: str) -> Any:
    return Upload(sample(name), name)


async def commit(db: AsyncSession, name: str, choices: str = "[]") -> api.BatchOut:
    return await api.commit_combined(upload(name), db, session_of(), None, choices)


async def number(db: AsyncSession, model: Any) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


async def document_of(db: AsyncSession, client: str, on: date) -> OrderDocument:
    return (
        await db.execute(
            select(OrderDocument)
            .join(Order, Order.id == OrderDocument.order_id)
            .join(Client, Client.id == Order.client_id)
            .where(Client.name == client, Order.order_date == on)
        )
    ).scalar_one()


async def id_of(db: AsyncSession, model: Any, name: str) -> int:
    return (await db.execute(select(model.id).where(model.name == name))).scalar_one()


def test_the_combined_routes_come_before_the_kind_routes_they_would_match() -> None:
    paths = [route.path for route in api.router.routes]  # type: ignore[attr-defined]
    assert paths.index("/imports/combined/analyse") < paths.index("/imports/{kind}/analyse")
    assert paths.index("/imports/combined/preview") < paths.index("/imports/{kind}/preview")
    assert paths.index("/imports/combined/commit") < paths.index("/imports/{kind}/commit")


@pytest.mark.asyncio
async def test_analyse_lists_every_sheet_with_a_guess_for_every_kind(db: AsyncSession) -> None:
    out = await api.analyse_combined(upload(SAMPLE_1), None)
    assert [s.kind for s in out.sheets] == ["orders", "clients", "translators", "notaries", None]
    orders = out.sheets[0]
    assert orders.columns[0].header == "თარიღი"
    assert orders.columns[0].sample[0] == "2026-01-12"
    assert set(orders.suggested) == {"clients", "translators", "notaries", "orders"}
    assert orders.suggested["orders"] == orders.mapping
    assert {f.key for f in out.fields["notaries"]} >= {"registration_number", "bank_iban"}
    flat = await api.analyse_combined(upload(SAMPLE_2), None)
    assert flat.sheets[0].skipped_count == 4
    assert flat.sheets[0].skipped[-1].reason == "heading"


@pytest.mark.asyncio
async def test_preview_counts_each_sheet_and_what_would_be_created_and_saves_nothing(
    db: AsyncSession,
) -> None:
    out = await api.preview_combined(upload(SAMPLE_1), db, None)
    assert [(s.name, s.will_create, s.duplicates, s.errors) for s in out.sheets] == [
        ("შეკვეთები 2026", 13, 1, 1),
        ("კლიენტები", 10, 1, 1),
        ("თარჯიმნები", 7, 1, 0),
        ("ნოტარიუსები", 5, 1, 0),
    ]
    assert (out.total, out.will_create, out.duplicates, out.errors) == (41, 35, 4, 2)
    assert out.creates.model_dump() == {
        "clients": 11,
        "translators": 7,
        "notaries": 5,
        "orders": 13,
    }
    assert (await number(db, Client), await number(db, Order), await number(db, Notary)) == (
        0,
        0,
        0,
    )


@pytest.mark.asyncio
async def test_commit_saves_the_people_before_the_orders_and_links_them(db: AsyncSession) -> None:
    batch = await commit(db, SAMPLE_1)
    assert (batch.kind, batch.rows_total, batch.created_count) == ("combined", 41, 35)
    assert (batch.duplicate_count, batch.error_count) == (4, 2)
    assert (await number(db, Client), await number(db, Translator)) == (11, 7)
    assert (await number(db, Notary), await number(db, Order)) == (5, 13)
    # Eka's pairs came with her, both ways.
    eka = await id_of(db, Translator, "ეკა წულაძე")
    pairs = (
        await db.execute(
            select(
                TranslatorLanguagePair.source_language, TranslatorLanguagePair.target_language
            ).where(TranslatorLanguagePair.translator_id == eka)
        )
    ).all()
    assert set(pairs) == {("ka", "en"), ("en", "ka")}
    # The first order of the file points at the translator the file's own sheet created.
    document = await document_of(db, "ნინო ქავთარაძე", date(2026, 1, 12))
    assert document.translator_id == eka
    assert document.translator_cost == Decimal("24.00")
    # And the client only the orders knew about is a real client with both their orders.
    zurab = await id_of(db, Client, "ზურაბ ჩხეიძე")
    assert await number(db, select(Order).where(Order.client_id == zurab).subquery()) == 2


@pytest.mark.asyncio
async def test_a_flat_sheet_creates_its_clients_translators_and_notaries_and_links_each_order(
    db: AsyncSession,
) -> None:
    batch = await commit(db, SAMPLE_2)
    assert (batch.rows_total, batch.created_count, batch.duplicate_count, batch.error_count) == (
        20,
        18,
        1,
        1,
    )
    assert (await number(db, Client), await number(db, Translator)) == (10, 3)
    assert (await number(db, Notary), await number(db, Order)) == (3, 18)
    document = await document_of(db, "სოფიო მჭედლიშვილი", date(2026, 3, 2))
    assert document.translator_id == await id_of(db, Translator, "ეკა წულაძე")
    assert document.notary_id == await id_of(db, Notary, "თეონა გოგოლაძე")
    assert (document.price, document.translator_cost, document.notary_cost) == (
        Decimal("40.00"),
        Decimal("24.00"),
        Decimal("15.00"),
    )
    assert document.is_notarized is True
    # No notary named: nothing to link, and the order is not notarized.
    plain = await document_of(db, "Андрей Петров", date(2026, 3, 20))
    assert (plain.notary_id, plain.is_notarized) == (None, False)
    # A translator who appears only by name after the first row is the same person.
    zaza = await id_of(db, Translator, "ზაზა მაისურაძე")
    assert (await document_of(db, "Hans Müller", date(2026, 3, 12))).translator_id == zaza
    assert (
        await db.execute(select(Translator.phone).where(Translator.id == zaza))
    ).scalar_one() == "574 61 61 61"


@pytest.mark.asyncio
async def test_importing_the_same_file_again_creates_nothing_and_says_so(db: AsyncSession) -> None:
    await commit(db, SAMPLE_2)
    with pytest.raises(ValidationError, match="Nothing to import"):
        await commit(db, SAMPLE_2)
    assert (await number(db, Client), await number(db, Order)) == (10, 18)


@pytest.mark.asyncio
async def test_a_second_file_matches_the_people_the_first_created(db: AsyncSession) -> None:
    await commit(db, SAMPLE_1)
    out = await api.preview_combined(upload(SAMPLE_2), db, None)
    # Nino and Mariam, Eka, Theona and Nestan are already there.
    assert out.creates.model_dump() == {"clients": 8, "translators": 2, "notaries": 1, "orders": 18}
    await commit(db, SAMPLE_2)
    assert (await number(db, Translator), await number(db, Notary)) == (9, 6)
    nino = await id_of(db, Client, "ნინო ქავთარაძე")
    assert (await document_of(db, "ნინო ქავთარაძე", date(2026, 3, 9))).order_id in (
        await db.execute(select(Order.id).where(Order.client_id == nino))
    ).scalars().all()
    assert await number(db, select(Client).where(Client.name == "ნინო ქავთარაძე").subquery()) == 1


@pytest.mark.asyncio
async def test_undo_takes_back_the_whole_import_orders_people_and_pairs(db: AsyncSession) -> None:
    batch = await commit(db, SAMPLE_1)
    out = await api.undo(batch.id, db, session_of(), None)
    assert out.undone_at is not None
    for model in (Client, Translator, TranslatorLanguagePair, Notary, Order, OrderDocument):
        assert await number(db, model) == 0, model.__name__


@pytest.mark.asyncio
async def test_undo_of_a_flat_sheet_removes_the_people_it_introduced(db: AsyncSession) -> None:
    batch = await commit(db, SAMPLE_2)
    await api.undo(batch.id, db, session_of(), None)
    for model in (Client, Translator, Notary, Order, OrderDocument):
        assert await number(db, model) == 0, model.__name__


@pytest.mark.asyncio
async def test_undo_is_refused_when_an_order_made_since_uses_an_imported_translator(
    db: AsyncSession,
) -> None:
    batch = await commit(db, SAMPLE_1)
    client = await id_of(db, Client, "ნინო ქავთარაძე")
    order = Order(client_id=client, order_date=date(2026, 11, 1), created_by_user_id=1)
    db.add(order)
    await db.flush()
    db.add(
        OrderDocument(
            order_id=order.id,
            document_type_id=1,
            source_language="ka",
            target_language="en",
            price=Decimal("10"),
            translator_id=await id_of(db, Translator, "ეკა წულაძე"),
        )
    )
    await db.flush()
    with pytest.raises(ConflictError, match="other orders"):
        await api.undo(batch.id, db, session_of(), None)
    # Nothing was deleted on the way to the refusal.
    assert (await number(db, Translator), await number(db, Order)) == (7, 14)


@pytest.mark.asyncio
async def test_undo_is_refused_when_an_imported_notary_is_on_another_order(
    db: AsyncSession,
) -> None:
    batch = await commit(db, SAMPLE_2)
    client = await id_of(db, Client, "ანა სვანიძე")
    order = Order(client_id=client, order_date=date(2026, 11, 1), created_by_user_id=1)
    db.add(order)
    await db.flush()
    db.add(
        OrderDocument(
            order_id=order.id,
            document_type_id=1,
            source_language="ka",
            target_language="en",
            price=Decimal("10"),
            notary_id=await id_of(db, Notary, "თეონა გოგოლაძე"),
            notary_cost=Decimal("5"),
        )
    )
    await db.flush()
    with pytest.raises(ConflictError):
        await api.undo(batch.id, db, session_of(), None)
    assert await number(db, Notary) == 3


@pytest.mark.asyncio
async def test_the_choices_of_the_person_decide_what_is_imported(db: AsyncSession) -> None:
    skip_orders = json.dumps([{"index": 0, "skip": True}])
    out = await api.preview_combined(upload(SAMPLE_1), db, None, skip_orders)
    assert [s.kind for s in out.sheets] == ["clients", "translators", "notaries"]
    assert out.creates.orders == 0
    # Without orders there is nothing to say who the translators are: the sheet does.
    batch = await commit(db, SAMPLE_1, skip_orders)
    assert (await number(db, Order), await number(db, Client)) == (0, 10)
    assert batch.kind == "combined"


@pytest.mark.asyncio
async def test_choices_that_cannot_be_read_are_refused(db: AsyncSession) -> None:
    for bad in ("not json", "{}", '[{"kind": "orders"}]', '[{"index": 0, "kind": "robots"}]'):
        with pytest.raises(ValidationError):
            await api.preview_combined(upload(SAMPLE_1), db, None, bad)


# ── Notaries on their own ───────────────────────────────────────────────────

NOTARIES_CSV = (
    "Notary,Phone,IBAN,Registration number\n"
    "Theona,555901020,GE00TB0000000000000101,NT-1\n"
    "Vakhtang,599802030,,NT-2\n"
    "Theona again,555 90 10 20,,\n"
)


@pytest.mark.asyncio
async def test_a_notaries_sheet_is_an_import_of_its_own_and_can_be_undone(db: AsyncSession) -> None:
    mapping = json.dumps({"name": 0, "phone": 1, "bank_iban": 2, "registration_number": 3})
    out = await api.preview("notaries", Upload(csv_bytes(NOTARIES_CSV), "n.csv"), mapping, db, None)  # type: ignore[arg-type]
    assert (out.will_create, out.duplicates) == (2, 1)
    assert "no IBAN" in out.sample[1].summary
    batch = await api.commit(
        "notaries", Upload(csv_bytes(NOTARIES_CSV), "n.csv"), mapping, db, session_of(), None
    )  # type: ignore[arg-type]
    assert await number(db, Notary) == 2
    await api.undo(batch.id, db, session_of(), None)
    assert await number(db, Notary) == 0
    assert (await db.get(ImportBatch, batch.id)).undone_at is not None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_orders_on_their_own_now_take_the_notary_and_their_fee(db: AsyncSession) -> None:
    db.add(Notary(name="Theona", phone="555901020"))
    await db.flush()
    csv = (
        "Client,Date,Languages,Price,Notary,Notary fee\n"
        "Nino,25.10.2025,ka-en,40,Theona,15\n"
        "Gia,26.10.2025,ka-en,30,Stranger,5\n"
    )
    mapping = {
        "client_name": 0,
        "order_date": 1,
        "language_pair": 2,
        "price": 3,
        "notary": 4,
        "notary_cost": 5,
    }
    out = await api.commit(
        "orders", Upload(csv_bytes(csv), "o.csv"), json.dumps(mapping), db, session_of(), None
    )  # type: ignore[arg-type]
    assert out.created_count == 2
    first = await document_of(db, "Nino", date(2025, 10, 25))
    assert first.notary_id == await id_of(db, Notary, "Theona")
    assert (first.notary_cost, first.is_notarized) == (Decimal("15.00"), True)
    # A notary the bureau does not have is not invented by a plain orders import.
    second = await document_of(db, "Gia", date(2025, 10, 26))
    assert (second.notary_id, second.notary_cost) == (None, Decimal("5.00"))
