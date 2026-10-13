"""Importing clients and translators from a spreadsheet.

The engine (``domain/imports.py``) is pure and tested directly. The endpoints
run for real against in-memory SQLite, like ``test_translator_profile.py``.
"""

# ruff: noqa: E501, DTZ001  (test data rows are wide; calendar dates need no zone)

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from suliko.api.v1 import imports as api
from suliko.core.errors import ConflictError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain import imports as engine
from suliko.domain.plans import TenantPlan
from suliko.models.collaboration import OrderComment
from suliko.models.directory import Client, Translator, TranslatorLanguagePair
from suliko.models.finance import (
    ClientPayment,
    ClientPaymentAllocation,
    PaymentMethod,
    TranslatorPayment,
    TranslatorPaymentAllocation,
)
from suliko.models.imports import ImportBatch, ImportBatchItem
from suliko.models.order import Order, OrderDocument, OrderStatusEvent
from suliko.models.order_file import OrderFile
from suliko.models.reference import CustomOption, DocumentType, Language
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role
from suliko.security.permissions import permissions_for_role
from suliko.security.sessions import AuthenticatedSession

install_tenant_filter()

ACME = 1


def workbook(rows: list[list[Any]]) -> bytes:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def csv_bytes(text: str) -> bytes:
    return text.encode("utf-8-sig")


# ── Reading the file ────────────────────────────────────────────────────────


def test_an_xlsx_is_read_with_its_header_and_numbered_rows() -> None:
    sheet = engine.read_sheet(
        workbook([["Name", "Phone"], ["Nino", 555123456], [], ["Gia", None]]), "a.xlsx"
    )
    assert sheet.headers == ["Name", "Phone"]
    # The blank row is skipped, and the numbers are the spreadsheet's own.
    assert [number for number, _ in sheet.rows] == [2, 4]


def test_a_csv_is_read_whatever_the_separator() -> None:
    for separator in (",", ";", "\t"):
        text = f"Name{separator}Phone\nNino{separator}555123456\n"
        sheet = engine.read_sheet(csv_bytes(text), "a.csv")
        assert sheet.headers == ["Name", "Phone"]
        assert sheet.rows[0][1][0] == "Nino"


def test_georgian_text_survives_a_csv() -> None:
    sheet = engine.read_sheet(csv_bytes("სახელი,ტელეფონი\nნინო,555123456\n"), "a.csv")
    assert sheet.headers == ["სახელი", "ტელეფონი"]
    assert sheet.rows[0][1][0] == "ნინო"


@pytest.mark.parametrize(
    ("content", "name"),
    [
        (b"", "a.csv"),
        (b"Name\n", "a.csv"),  # a header and nothing under it
        (b"not a workbook", "a.xlsx"),
    ],
)
def test_unusable_files_are_refused_with_a_sentence(content: bytes, name: str) -> None:
    with pytest.raises(ValidationError):
        engine.read_sheet(content, name)


def test_a_file_over_the_size_limit_is_refused() -> None:
    with pytest.raises(ValidationError, match="larger than"):
        engine.read_sheet(b"x" * (engine.MAX_BYTES + 1), "a.csv")


def test_the_old_xls_format_is_refused_with_advice() -> None:
    with pytest.raises(ValidationError, match="xlsx"):
        engine.read_sheet(b"\xd0\xcf\x11\xe0" + b"x" * 50, "old.xls")


def test_too_many_rows_are_refused() -> None:
    text = "Name\n" + "\n".join(f"n{i}" for i in range(engine.MAX_ROWS + 1))
    with pytest.raises(ValidationError, match="at most"):
        engine.read_sheet(csv_bytes(text), "big.csv")


# ── Guessing the columns ────────────────────────────────────────────────────


def test_headers_in_georgian_english_and_russian_are_recognised() -> None:
    guessed = engine.suggest_mapping(
        "clients", ["სახელი გვარი", "Mobile", "E-mail", "პირადი ნომერი", "Адрес", "Random"]
    )
    assert guessed == {"name": 0, "phone": 1, "email": 2, "personal_number": 3, "address": 4}


def test_a_column_serves_one_field_only() -> None:
    guessed = engine.suggest_mapping("clients", ["Name", "Name"])
    assert guessed == {"name": 0}


def test_translator_headers_find_languages_and_cities() -> None:
    guessed = engine.suggest_mapping("translators", ["Translator", "ენები", "ქალაქი", "Notary"])
    assert guessed == {"name": 0, "language_pairs": 1, "cities": 2, "can_notarize": 3}


def test_a_mapping_must_name_the_name_and_use_each_column_once() -> None:
    with pytest.raises(ValidationError, match="name"):
        engine.check_mapping("clients", {"phone": 0}, 3)
    with pytest.raises(ValidationError, match="one field"):
        engine.check_mapping("clients", {"name": 0, "phone": 0}, 3)
    with pytest.raises(ValidationError, match="not in the file"):
        engine.check_mapping("clients", {"name": 7}, 3)
    with pytest.raises(ValidationError, match="Unknown"):
        engine.check_mapping("clients", {"name": 0, "colour": 1}, 3)


# ── Cleaning cells ──────────────────────────────────────────────────────────


def test_numbers_excel_stored_as_floats_are_not_shown_with_a_point_zero() -> None:
    assert engine.cell_text(555123456.0) == "555123456"
    assert engine.cell_text(None) == ""
    assert engine.cell_text("  a   b ") == "a b"  # noqa: RUF001


def test_one_phone_number_has_one_key_however_it_is_typed() -> None:
    keys = {engine.phone_key(p) for p in ("+995 555 12-34-56", "995555123456", "555123456")}
    assert keys == {"555123456"}
    assert engine.phone_key("12") is None


def test_amounts_with_a_decimal_comma_or_a_currency_are_read() -> None:
    assert str(engine.parse_rate("25,50")) == "25.50"
    assert str(engine.parse_rate("25.5 GEL")) == "25.5"
    assert engine.parse_rate("abc") is None
    assert engine.parse_rate("1.2.3") is None


def test_yes_and_no_in_three_languages() -> None:
    assert engine.parse_bool("კი") is True
    assert engine.parse_bool("Yes") is True
    assert engine.parse_bool("არა") is False
    assert engine.parse_bool("maybe") is None


def test_cities_are_found_by_code_english_or_georgian() -> None:
    codes, unknown = engine.parse_cities("Tbilisi, ბათუმი; Atlantis")
    assert codes == ["tbilisi", "batumi"]
    assert unknown == ["Atlantis"]


def lookup(word: str) -> str | None:
    return {"ka": "ka", "georgian": "ka", "ქართული": "ka", "en": "en", "english": "en"}.get(
        word.casefold()
    )


def test_language_pairs_are_read_from_codes_and_names() -> None:
    pairs, problems = engine.parse_pairs("ka-en; English > ქართული; ka-ka; nonsense", lookup)
    assert pairs == [("ka", "en"), ("en", "ka")]
    assert problems == ["ka-ka", "nonsense"]


# ── Judging rows ────────────────────────────────────────────────────────────


def sheet_of(*rows: list[Any]) -> engine.Sheet:
    return engine.Sheet(headers=["name", "phone", "email"], rows=list(enumerate(rows, start=2)))


MAP = {"name": 0, "phone": 1, "email": 2}


def judge(
    *rows: list[Any], existing: list[engine.ExistingPerson] | None = None
) -> list[engine.RowResult]:
    return engine.judge_sheet("clients", sheet_of(*rows), MAP, existing or []).results


def test_a_good_row_is_ok_and_a_company_is_recognised_by_its_name() -> None:
    (person, company) = judge(
        ["Nino", "555 12 34 56", "NINO@Example.com"], ["Acme LLC", None, None]
    )
    assert person.status == "ok"
    assert person.values["email"] == "nino@example.com"
    assert person.values["client_type"] == "B2C"
    assert company.values["client_type"] == "B2B"


def test_a_row_without_a_name_is_an_error() -> None:
    (row,) = judge([None, "555123456", None])
    assert row.status == "error"
    assert row.messages == ["No name."]


def test_a_bad_email_or_phone_is_left_out_with_a_warning_not_a_refusal() -> None:
    (row,) = judge(["Nino", "n/a", "not-an-email"])
    assert row.status == "ok"
    assert row.values["email"] is None
    assert row.values["phone"] is None
    assert len(row.warnings) == 2


def test_an_existing_client_makes_the_row_a_duplicate_and_names_it() -> None:
    existing = [engine.ExistingPerson(label="#7 Gia", phone="+995 555 123 456")]
    (row,) = judge(["Somebody else", "555123456", None], existing=existing)
    assert row.status == "duplicate"
    assert row.matched == "#7 Gia"
    assert "phone" in row.messages[0]


def test_the_same_person_twice_in_the_file_is_one_row() -> None:
    first, second = judge(["Nino", None, "n@x.ge"], ["Nino B.", None, "N@X.ge"])
    assert (first.status, second.status) == ("ok", "duplicate")


def test_two_people_sharing_nothing_are_both_created() -> None:
    a, b = judge(["Nino", "555111222", None], ["Gia", "555333444", None])
    assert (a.status, b.status) == ("ok", "ok")


def translators(
    *rows: list[Any], existing: list[engine.ExistingPerson] | None = None
) -> list[engine.RowResult]:
    sheet = engine.Sheet(
        headers=["name", "phone", "languages", "cities", "notary"],
        rows=list(enumerate(rows, start=2)),
    )
    mapping = {"name": 0, "phone": 1, "language_pairs": 2, "cities": 3, "can_notarize": 4}
    return engine.judge_sheet("translators", sheet, mapping, existing or [], lookup).results


def test_a_translator_row_carries_pairs_cities_and_notary() -> None:
    (row,) = translators(["Mari", "555111222", "ka-en; en-ka", "Tbilisi", "კი"])
    assert row.status == "ok"
    assert row.values["language_pairs"] == [("ka", "en"), ("en", "ka")]
    assert row.values["cities"] == ["tbilisi"]
    assert row.values["can_notarize"] is True


def test_a_translator_with_the_same_name_is_a_duplicate_even_without_contact_details() -> None:
    existing = [engine.ExistingPerson(label="Mari", name="mari")]
    (row,) = translators(["Mari", None, None, None, None], existing=existing)
    assert row.status == "duplicate"


def test_pairs_not_understood_are_warned_about_and_the_row_is_still_created() -> None:
    (row,) = translators(["Mari", None, "klingon-en", None, None])
    assert row.status == "ok"
    assert row.values["language_pairs"] == []
    assert row.warnings


# ── The endpoints ───────────────────────────────────────────────────────────

MODELS = (
    Tenant,
    Language,
    Client,
    Translator,
    TranslatorLanguagePair,
    ImportBatch,
    ImportBatchItem,
    Order,
    OrderDocument,
    ClientPayment,
    TranslatorPayment,
    ClientPaymentAllocation,
    TranslatorPaymentAllocation,
    OrderStatusEvent,
    OrderComment,
    OrderFile,
    DocumentType,
    CustomOption,
)


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
        username="owner",
        full_name="Owner",
        email="o@acme.test",
        role=Role.OWNER,
        tenant_id=ACME,
        tenant_slug="acme",
        tenant_name="Acme",
        plan=TenantPlan.BUREAU,
        onboarding_required=False,
        must_change_password=False,
        has_mfa=False,
        permissions=permissions_for_role(Role.OWNER),
        mfa_satisfied_at=None,
        impersonated_by_user_id=None,
    )


class Upload:
    """What FastAPI hands a handler for a file part."""

    def __init__(self, content: bytes, filename: str) -> None:
        self._io = io.BytesIO(content)
        self.filename = filename
        self.content_type = "text/csv"

    async def read(self, size: int = -1) -> bytes:
        return self._io.read(size)


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
            for code, en, ka in (("ka", "Georgian", "ქართული"), ("en", "English", "ინგლისური")):
                session.add(Language(tenant_id=ACME, code=code, name_en=en, name_ka=ka))
            session.add(DocumentType(tenant_id=ACME, name_en="Passport", name_ka="პასპორტი"))
            session.add(DocumentType(tenant_id=ACME, name_en="Diploma", name_ka="დიპლომა"))
            await session.commit()
        with tenant_scope(ACME):
            yield session
    await engine_.dispose()


CLIENT_CSV = (
    "Name,Phone,Email\n"
    "Nino,555111222,nino@x.ge\n"
    "Gia,555333444,\n"
    ",555999000,\n"
    "Nino again,555111222,\n"
)


async def commit_clients(db: AsyncSession) -> api.BatchOut:
    return await api.commit(
        "clients",
        Upload(csv_bytes(CLIENT_CSV), "clients.csv"),  # type: ignore[arg-type]
        json.dumps({"name": 0, "phone": 1, "email": 2}),
        db,
        _session(),
        None,
    )


@pytest.mark.asyncio
async def test_analyse_returns_columns_samples_and_a_guess(db: AsyncSession) -> None:
    out = await api.analyse("clients", Upload(csv_bytes(CLIENT_CSV), "c.csv"), None)  # type: ignore[arg-type]
    assert [c.header for c in out.columns] == ["Name", "Phone", "Email"]
    assert out.columns[0].sample[:2] == ["Nino", "Gia"]
    assert out.suggested == {"name": 0, "phone": 1, "email": 2}
    assert out.total_rows == 4


@pytest.mark.asyncio
async def test_preview_counts_and_saves_nothing(db: AsyncSession) -> None:
    out = await api.preview(
        "clients",
        Upload(csv_bytes(CLIENT_CSV), "c.csv"),  # type: ignore[arg-type]
        json.dumps({"name": 0, "phone": 1}),
        db,
        None,
    )
    assert (out.total, out.will_create, out.duplicates, out.errors) == (4, 2, 1, 1)
    assert {p.status for p in out.problems} == {"duplicate", "error"}
    assert await db.scalar(select(func.count()).select_from(Client)) == 0


@pytest.mark.asyncio
async def test_commit_saves_only_the_good_rows_and_records_the_batch(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    batch = await commit_clients(db)
    assert (batch.created_count, batch.duplicate_count, batch.error_count) == (2, 1, 1)
    names = set((await db.execute(select(Client.name))).scalars())
    assert names == {"Nino", "Gia"}
    items = await db.scalar(select(func.count()).select_from(ImportBatchItem))
    assert items == 2
    assert [c["action"] for c in audit] == ["import.committed"]


@pytest.mark.asyncio
async def test_importing_the_same_file_again_creates_nothing_and_says_so(db: AsyncSession) -> None:
    await commit_clients(db)
    with pytest.raises(ValidationError, match="Nothing to import"):
        await commit_clients(db)
    assert await db.scalar(select(func.count()).select_from(Client)) == 2


@pytest.mark.asyncio
async def test_undo_removes_what_the_import_created(db: AsyncSession) -> None:
    batch = await commit_clients(db)
    undone = await api.undo(batch.id, db, _session(), None)
    assert undone.undone_at is not None and not undone.can_undo
    assert await db.scalar(select(func.count()).select_from(Client)) == 0
    with pytest.raises(ConflictError, match="already undone"):
        await api.undo(batch.id, db, _session(), None)


@pytest.mark.asyncio
async def test_a_client_with_an_order_stops_the_undo(db: AsyncSession) -> None:
    batch = await commit_clients(db)
    client_id = await db.scalar(select(Client.id).where(Client.name == "Nino"))
    from datetime import date

    db.add(Order(client_id=client_id, order_date=date(2026, 10, 1), created_by_user_id=1))
    await db.flush()
    with pytest.raises(ConflictError, match="can no longer be undone"):
        await api.undo(batch.id, db, _session(), None)
    assert await db.scalar(select(func.count()).select_from(Client)) == 2


@pytest.mark.asyncio
async def test_only_the_latest_import_can_be_undone(db: AsyncSession) -> None:
    first = await commit_clients(db)
    await api.commit(
        "clients",
        Upload(csv_bytes("Name\nSomeone Else\n"), "more.csv"),  # type: ignore[arg-type]
        json.dumps({"name": 0}),
        db,
        _session(),
        None,
    )
    with pytest.raises(ConflictError, match="latest"):
        await api.undo(first.id, db, _session(), None)
    history = await api.history(db, None)
    assert [b.can_undo for b in history] == [True, False]


@pytest.mark.asyncio
async def test_translators_come_with_their_pairs_and_undo_takes_them_away(db: AsyncSession) -> None:
    text = "Name,Languages,City\nMari,ka-en; English > Georgian,Tbilisi\n"
    batch = await api.commit(
        "translators",
        Upload(csv_bytes(text), "t.csv"),  # type: ignore[arg-type]
        json.dumps({"name": 0, "language_pairs": 1, "cities": 2}),
        db,
        _session(),
        None,
    )
    assert batch.created_count == 1
    translator = (await db.execute(select(Translator))).scalar_one()
    assert translator.cities == ",tbilisi,"
    pairs = (await db.execute(select(TranslatorLanguagePair))).scalars().all()
    assert {(p.source_language, p.target_language) for p in pairs} == {("ka", "en"), ("en", "ka")}

    await api.undo(batch.id, db, _session(), None)
    assert await db.scalar(select(func.count()).select_from(Translator)) == 0
    assert await db.scalar(select(func.count()).select_from(TranslatorLanguagePair)) == 0


@pytest.mark.asyncio
async def test_an_unknown_kind_is_not_found(db: AsyncSession) -> None:
    from suliko.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await api.analyse("invoices", Upload(csv_bytes("a\n1\n"), "x.csv"), None)  # type: ignore[arg-type]


# ── Orders: the engine ──────────────────────────────────────────────────────

from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402

from suliko.domain.imports_orders import ExistingOrder, OrdersContext  # noqa: E402


def test_dates_are_read_day_first_from_text_and_excel_cells() -> None:
    from datetime import datetime

    assert engine.parse_date("25.10.2025") == date(2025, 10, 25)
    assert engine.parse_date("25/10/2025") == date(2025, 10, 25)
    assert engine.parse_date("2025-10-25") == date(2025, 10, 25)
    assert engine.parse_date(datetime(2025, 10, 25, 14, 30)) == date(2025, 10, 25)
    assert engine.parse_date("31.02.2025") is None
    assert engine.parse_date("soon") is None
    assert engine.parse_date("25.10.1925") is None  # a typo, not an order


def test_pages_are_read_from_text() -> None:
    assert engine.parse_int("3") == 3
    assert engine.parse_int("3.0") == 3
    assert engine.parse_int("3 pages") == 3
    assert engine.parse_int("n/a") is None


HEADERS = [
    "Client", "Phone", "Date", "Languages", "Document", "Pages", "Price", "Translator", "Status", "Paid", "Tender",
]  # fmt: skip
MAPPING = {
    "client_name": 0, "client_phone": 1, "order_date": 2, "language_pair": 3, "document_type": 4,
    "pages": 5, "price": 6, "translator": 7, "status": 8, "paid": 9, "identifier": 10,
}  # fmt: skip


def orders_ctx(existing: list[ExistingOrder] | None = None) -> OrdersContext:
    return OrdersContext(
        language_lookup=lookup,
        document_types={"passport": (1, "Passport"), "პასპორტი": (1, "Passport")},
        default_document_type=(1, "Passport"),
        translators={"mari": (7, "Mari")},
        status_lookup=lambda text: {"completed": "completed", "delivered": "completed"}.get(
            text.casefold()
        ),
        existing_orders=existing or [],
    )


def order_rows(
    *rows: list[Any],
    clients: list[engine.ExistingPerson] | None = None,
    ctx: OrdersContext | None = None,
) -> list[engine.RowResult]:
    sheet = engine.Sheet(headers=HEADERS, rows=list(enumerate(rows, start=2)))
    return engine.judge_sheet(
        "orders", sheet, MAPPING, clients or [], lookup, orders=ctx or orders_ctx()
    ).results


def row(**fields: Any) -> list[Any]:
    base = {
        "Client": "Nino", "Phone": "555111222", "Date": "25.10.2025", "Languages": "ka-en",
        "Document": "Passport", "Pages": 2, "Price": "40", "Translator": None, "Status": None,
        "Paid": None, "Tender": None,
    }  # fmt: skip
    base.update(fields)
    return [base[h] for h in HEADERS]


def test_a_good_row_is_an_order_with_a_new_client() -> None:
    (result,) = order_rows(row())
    assert result.status == "ok"
    v = result.values
    assert v["client"]["new"]["name"] == "Nino"
    assert (v["source_language"], v["target_language"]) == ("ka", "en")
    assert (v["page_count"], v["price"], v["document_type_id"]) == (2, Decimal("40"), 1)
    assert v["order_date"] == date(2025, 10, 25)


def test_the_same_client_twice_is_one_new_client() -> None:
    first, second = order_rows(row(), row(Date="26.10.2025"))
    assert first.values["client"]["ref"] == second.values["client"]["ref"] == "new:0"
    assert first.values["client"]["new"] is not None
    assert second.values["client"]["new"] is None


def test_a_client_the_bureau_has_is_matched_by_phone() -> None:
    known = [engine.ExistingPerson(label="#7 Nino B.", phone="+995 555 111 222", ref=7)]
    (result,) = order_rows(row(Client="Someone else"), clients=known)
    assert result.values["client"] == {"ref": 7, "new": None}
    assert result.name == "#7 Nino B."


def test_a_client_matched_by_name_only_is_flagged() -> None:
    known = [engine.ExistingPerson(label="#7 Nino", name="Nino", ref=7)]
    (result,) = order_rows(row(Phone=None), clients=known)
    assert result.values["client"]["ref"] == 7
    assert any("matched by name only" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"Date": "later"}, "not a date"),
        ({"Date": None}, "No order date"),
        ({"Languages": "ka-xx"}, "languages"),
        ({"Price": None}, "No price"),
        ({"Price": "free"}, "not an amount"),
        ({"Client": None, "Phone": None}, "No client"),
    ],
)
def test_rows_that_cannot_be_orders_are_errors_with_a_reason(
    fields: dict[str, Any], message: str
) -> None:
    (result,) = order_rows(row(**fields))
    assert result.status == "error"
    assert message.lower() in result.messages[0].lower()


def test_a_bad_row_leaves_no_client_behind_for_the_next_one() -> None:
    bad, good = order_rows(row(Price=None), row())
    assert bad.status == "error"
    assert good.values["client"] == {"ref": "new:0", "new": good.values["client"]["new"]}
    assert good.values["client"]["new"] is not None


def test_things_not_recognised_are_warnings_and_the_row_still_imports() -> None:
    (result,) = order_rows(
        row(Document="Contract", Translator="Zaza", Status="weird", Pages="many")
    )
    assert result.status == "ok"
    assert len(result.warnings) == 4
    v = result.values
    assert (v["document_type_id"], v["translator_id"], v["status"], v["page_count"]) == (
        1,
        None,
        None,
        1,
    )


def test_paid_more_than_the_price_is_capped_and_a_status_is_found() -> None:
    (result,) = order_rows(row(Paid="100", Status="Delivered", Translator="Mari"))
    assert result.values["paid"] == Decimal("40")
    assert result.values["status"] == "completed"
    assert result.values["translator_id"] == 7
    assert any("more than the price" in w for w in result.warnings)


def test_the_same_order_in_the_file_twice_is_a_duplicate() -> None:
    first, second = order_rows(row(), row())
    assert (first.status, second.status) == ("ok", "duplicate")
    # A different tender number makes it a different order.
    a, b = order_rows(row(Tender="T-1"), row(Tender="T-2"))
    assert (a.status, b.status) == ("ok", "ok")


def test_an_order_already_in_the_bureau_is_a_duplicate() -> None:
    known = [engine.ExistingPerson(label="#7 Nino", phone="555111222", ref=7)]
    existing = [
        ExistingOrder(
            label="order #12",
            client_id=7,
            order_date=date(2025, 10, 25),
            source_language="ka",
            target_language="en",
            page_count=2,
            price=Decimal("40"),
        )
    ]
    (result,) = order_rows(row(), clients=known, ctx=orders_ctx(existing))
    assert result.status == "duplicate"
    assert result.matched == "order #12"


# ── Orders: the endpoints ───────────────────────────────────────────────────

ORDERS_CSV = (
    "Client,Phone,Date,Languages,Document,Pages,Price,Status,Paid,Tender\n"
    "Nino,555111222,25.10.2025,ka-en,Passport,2,40,Delivered,40,T-1\n"
    "Nino,555111222,26.10.2025,en-ka,Diploma,1,30,,0,\n"
    "Gia,,27.10.2025,ka-en,Passport,1,25,,10,\n"
    ",,,,,,,,,\n"
    "Bad,555999000,not a date,ka-en,Passport,1,25,,,\n"
)
ORDERS_MAPPING = {
    "client_name": 0, "client_phone": 1, "order_date": 2, "language_pair": 3, "document_type": 4,
    "pages": 5, "price": 6, "status": 7, "paid": 8, "identifier": 9,
}  # fmt: skip


async def commit_orders(db: AsyncSession) -> api.BatchOut:
    return await api.commit(
        "orders",
        Upload(csv_bytes(ORDERS_CSV), "orders.csv"),  # type: ignore[arg-type]
        json.dumps(ORDERS_MAPPING),
        db,
        _session(),
        None,
    )


async def count(db: AsyncSession, model: Any) -> int:
    return await db.scalar(select(func.count()).select_from(model)) or 0


@pytest.mark.asyncio
async def test_preview_of_orders_counts_each_kind_of_row(db: AsyncSession) -> None:
    out = await api.preview(
        "orders",
        Upload(csv_bytes(ORDERS_CSV), "orders.csv"),  # type: ignore[arg-type]
        json.dumps(ORDERS_MAPPING),
        db,
        None,
    )
    assert (out.total, out.will_create, out.errors) == (4, 3, 1)
    assert await count(db, Order) == 0
    assert out.sample[0].summary[0] == "2025-10-25"
    assert "new client" in out.sample[0].summary[-1]


@pytest.mark.asyncio
async def test_commit_creates_clients_orders_documents_history_and_payments(
    db: AsyncSession,
) -> None:
    batch = await commit_orders(db)
    assert batch.created_count == 3
    assert {c.name for c in (await db.execute(select(Client))).scalars()} == {"Nino", "Gia"}
    orders = (await db.execute(select(Order).order_by(Order.order_date))).scalars().all()
    assert [o.source for o in orders] == ["import"] * 3
    assert orders[0].identifier == "T-1"
    assert await count(db, OrderDocument) == 3
    # Each order starts New; the delivered one has a second event.
    statuses = (await db.execute(select(OrderStatusEvent.status))).scalars().all()
    assert sorted(statuses) == ["completed", "new", "new", "new"]
    # Paid 40 and 10 become payments allocated to their orders; the 0 does not.
    amounts = sorted(p.amount for p in (await db.execute(select(ClientPayment))).scalars())
    assert amounts == [Decimal("10.00"), Decimal("40.00")]
    assert await count(db, ClientPaymentAllocation) == 2


@pytest.mark.asyncio
async def test_importing_the_same_orders_again_creates_nothing(db: AsyncSession) -> None:
    await commit_orders(db)
    with pytest.raises(ValidationError, match="Nothing to import"):
        await commit_orders(db)
    assert await count(db, Order) == 3


@pytest.mark.asyncio
async def test_undo_removes_the_orders_payments_and_the_clients_it_made(db: AsyncSession) -> None:
    batch = await commit_orders(db)
    await api.undo(batch.id, db, _session(), None)
    for model in (
        Order,
        OrderDocument,
        OrderStatusEvent,
        ClientPayment,
        ClientPaymentAllocation,
        Client,
    ):
        assert await count(db, model) == 0, model.__name__


@pytest.mark.asyncio
async def test_a_payment_made_since_stops_the_undo(db: AsyncSession) -> None:
    batch = await commit_orders(db)
    order = (await db.execute(select(Order).order_by(Order.order_date))).scalars().first()
    other = ClientPayment(
        client_id=order.client_id,
        amount=Decimal("5"),
        payment_date=date(2025, 11, 1),
        method=PaymentMethod.CASH,
        recorded_by_user_id=1,
    )
    db.add(other)
    await db.flush()
    db.add(
        ClientPaymentAllocation(
            payment_id=other.id, order_id=order.id, amount_allocated=Decimal("5")
        )
    )
    await db.flush()
    with pytest.raises(ConflictError, match="no longer be undone"):
        await api.undo(batch.id, db, _session(), None)
    assert await count(db, Order) == 3


@pytest.mark.asyncio
async def test_a_client_that_gained_an_order_since_is_kept_when_the_import_is_undone(
    db: AsyncSession,
) -> None:
    batch = await commit_orders(db)
    gia = await db.scalar(select(Client.id).where(Client.name == "Gia"))
    # Gia's imported order is undone with the batch; a second, later order of hers is not.
    db.add(Order(client_id=gia, order_date=date(2026, 1, 5), created_by_user_id=1))
    await db.flush()
    await api.undo(batch.id, db, _session(), None)
    remaining = (await db.execute(select(Client.name))).scalars().all()
    assert remaining == ["Gia"]
