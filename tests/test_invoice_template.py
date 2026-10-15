"""A bureau's own invoice: a Word file with placeholders, filled from an order."""

# ruff: noqa: RUF001  (the invoice prints a multiplication sign and a no-break space)

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from docx import Document
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from suliko.api.v1 import invoice_templates as api
from suliko.core.errors import NotFoundError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.domain import invoice_template as engine
from suliko.domain.plans import TenantPlan
from suliko.models.invoice import InvoiceTemplate
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role
from suliko.security.permissions import permissions_for_role
from suliko.security.sessions import AuthenticatedSession

install_tenant_filter()
ACME = 1


def template_bytes() -> bytes:
    """A small invoice: a header, a client line, a placeholder Word split in two
    runs, a table with one pattern row, and a placeholder nobody knows."""
    document = Document()
    document.sections[0].header.paragraphs[0].text = "{{seller_name}} / {{seller_id}}"
    document.add_paragraph("Invoice {{number}} for {{ client }}")
    split = document.add_paragraph()
    split.add_run("Total: {{to").bold = True
    split.add_run("tal}}")
    document.add_paragraph("Unknown: [{{bogus}}]")
    table = document.add_table(rows=2, cols=4)
    for cell, text in zip(table.rows[0].cells, ["#", "Document", "Qty", "Amount"], strict=True):
        cell.text = text
    for cell, text in zip(
        table.rows[1].cells,
        [
            "{{item.n}}",
            "{{item.description}} ({{item.languages}})",
            "{{item.quantity}}",
            "{{item.amount}}",
        ],
        strict=True,
    ):
        cell.text = text
    document.add_paragraph("Address:\n{{buyer_address}}")
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def lines_of(content: bytes) -> list[str]:
    document = Document(io.BytesIO(content))
    out = [p.text for p in document.paragraphs]
    out += [" | ".join(c.text for c in row.cells) for t in document.tables for row in t.rows]
    out += [p.text for p in document.sections[0].header.paragraphs]
    return out


def filled(locale: str = "en") -> list[str]:
    invoice = api.sample_invoice(locale)  # type: ignore[arg-type]
    values, items = engine.invoice_values(invoice, "Pay this")
    return lines_of(engine.fill_template(template_bytes(), values, items))


# ── The engine ──────────────────────────────────────────────────────────────


def test_placeholders_are_found_whatever_their_spacing_and_case() -> None:
    names = engine.check_template(template_bytes())
    assert {"seller_name", "number", "client", "total", "bogus", "item.n"} <= set(names)
    assert engine.unknown_placeholders(names) == ["bogus"]


def test_a_file_that_is_not_a_word_document_is_refused() -> None:
    with pytest.raises(ValidationError, match="Word document"):
        engine.check_template(b"%PDF-1.4 not a docx")
    with pytest.raises(ValidationError):
        engine.check_template(b"")
    with pytest.raises(ValidationError, match="larger"):
        engine.check_template(b"x" * (engine.MAX_TEMPLATE_BYTES + 1))


def test_every_placeholder_is_filled_in_the_body_and_the_header() -> None:
    out = filled()
    assert "Invoice 2610-SAMPLE for Giorgi Kapanadze" in out
    assert "Sample Translations LLC / 405000000" in out


def test_a_placeholder_split_across_runs_is_still_filled() -> None:
    assert "Total: 140.00 ₾" in filled()


def test_an_unknown_placeholder_prints_nothing() -> None:
    assert "Unknown: []" in filled()


def test_the_pattern_row_is_repeated_once_per_line_in_place() -> None:
    rows = [line for line in filled() if line.startswith(("1 |", "2 |", "# |"))]
    assert rows == [
        "# | Document | Qty | Amount",
        "1 | Passport (ka → en) | 2 | 40.00 ₾",
        "2 | Diploma (en → ka) | 3 | 90.00 ₾",
    ]
    assert not any("{{" in line for line in filled())


def test_minutes_and_characters_carry_their_unit_in_the_invoices_language() -> None:
    invoice = api.sample_invoice("ka")
    invoice.lines[0].unit = "minutes"
    invoice.lines[1].unit = "characters"
    invoice.lines[1].pages = 12500
    _, items = engine.invoice_values(invoice, "")
    assert [i["quantity"] for i in items] == ["2 წთ", "12 500 სიმბ."]


def test_the_items_placeholder_alone_in_a_paragraph_becomes_one_line_per_document() -> None:
    document = Document()
    document.add_paragraph("{{items}}")
    out = io.BytesIO()
    document.save(out)
    invoice = api.sample_invoice("en")
    values, items = engine.invoice_values(invoice, "")
    lines = [line for line in lines_of(engine.fill_template(out.getvalue(), values, items)) if line]
    assert lines == [
        "Passport, ka → en: 2 × 20.00 ₾ = 40.00 ₾",
        "Diploma, en → ka: 3 × 30.00 ₾ = 90.00 ₾",
    ]


def test_the_formatting_of_a_whole_placeholder_in_one_run_is_kept() -> None:
    document = Document()
    run = document.add_paragraph().add_run("{{client}}")
    run.bold = True
    out = io.BytesIO()
    document.save(out)
    invoice = api.sample_invoice("en")
    values, items = engine.invoice_values(invoice, "")
    result = Document(io.BytesIO(engine.fill_template(out.getvalue(), values, items)))
    (kept,) = result.paragraphs[0].runs
    assert (kept.text, kept.bold) == ("Giorgi Kapanadze", True)


# ── The endpoints ───────────────────────────────────────────────────────────


class Upload:
    def __init__(self, content: bytes, filename: str) -> None:
        self._io = io.BytesIO(content)
        self.filename = filename

    async def read(self, size: int = -1) -> bytes:
        return self._io.read(size)


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def record(_db: object, _session: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("suliko.core.audit.record", record)
    return calls


def session() -> AuthenticatedSession:
    return AuthenticatedSession(
        session_id=1, user_id=1, username="o", full_name="O", email="o@x.test", role=Role.OWNER,
        tenant_id=ACME, tenant_slug="acme", tenant_name="Acme", plan=TenantPlan.BUREAU,
        onboarding_required=False, must_change_password=False, has_mfa=False,
        permissions=permissions_for_role(Role.OWNER), mfa_satisfied_at=None,
        impersonated_by_user_id=None,
    )  # fmt: skip


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine_ = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine_.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(
                sync,
                tables=[Tenant.__table__, InvoiceTemplate.__table__],  # type: ignore[list-item]
            )
        )
    maker = async_sessionmaker(engine_, expire_on_commit=False, autoflush=False)
    async with maker() as s:
        with bypass_tenant_scope():
            s.add(
                Tenant(
                    id=ACME,
                    slug="acme",
                    display_name="Acme",
                    status=TenantStatus.ACTIVE,
                    locale="ka",
                )
            )
            await s.commit()
        with tenant_scope(ACME):
            yield s
    await engine_.dispose()


async def test_nothing_is_uploaded_to_begin_with_and_the_placeholders_are_listed(
    db: AsyncSession,
) -> None:
    out = await api.get_template(db, None)
    assert out.uploaded is False
    assert "total" in {p.name for p in out.placeholders}
    with pytest.raises(NotFoundError):
        await api.download_template(db, None)


async def test_an_upload_is_kept_replaced_and_reports_names_it_does_not_know(
    db: AsyncSession, audit: list[dict[str, Any]]
) -> None:
    out = await api.upload_template(
        Upload(template_bytes(), "My invoice.docx"), db, session(), None
    )  # type: ignore[arg-type]
    assert (out.uploaded, out.unknown) == (True, ["bogus"])
    assert out.file_name == "My invoice.docx"
    assert audit[-1]["action"] == "invoice_template.uploaded"
    # A second upload replaces the first: one per bureau.
    again = await api.upload_template(Upload(template_bytes(), "second.docx"), db, session(), None)  # type: ignore[arg-type]
    assert again.file_name == "second.docx"
    stored = await api.download_template(db, None)
    assert stored.body == template_bytes()


async def test_a_file_that_is_not_docx_is_not_kept(db: AsyncSession) -> None:
    with pytest.raises(ValidationError):
        await api.upload_template(Upload(b"nope", "x.docx"), db, session(), None)  # type: ignore[arg-type]
    assert (await api.get_template(db, None)).uploaded is False


async def test_the_sample_is_the_uploaded_file_filled_with_an_invented_invoice(
    db: AsyncSession,
) -> None:
    await api.upload_template(Upload(template_bytes(), "t.docx"), db, session(), None)  # type: ignore[arg-type]
    response = await api.sample(db, None, "en")
    assert response.media_type == engine.DOCX_TYPE
    assert "Invoice 2610-SAMPLE for Giorgi Kapanadze" in lines_of(response.body)


async def test_an_orders_invoice_is_filled_into_the_bureaus_file_or_is_not_found(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_build(*_args: Any) -> Any:
        return api.sample_invoice("en")

    monkeypatch.setattr(api, "build_invoice", fake_build)
    with pytest.raises(NotFoundError):
        await api.order_invoice_docx(5, db, session(), "en")
    await api.upload_template(Upload(template_bytes(), "t.docx"), db, session(), None)  # type: ignore[arg-type]
    response = await api.order_invoice_docx(5, db, session(), "en")
    assert "Total: 140.00 ₾" in lines_of(response.body)
    assert "invoice-2610-SAMPLE.docx" in response.headers["content-disposition"]


async def test_removing_it_goes_back_to_the_standard_layout(db: AsyncSession) -> None:
    await api.upload_template(Upload(template_bytes(), "t.docx"), db, session(), None)  # type: ignore[arg-type]
    out = await api.remove_template(db, session(), None)
    assert out.uploaded is False
    with pytest.raises(NotFoundError):
        await api.download_template(db, None)
