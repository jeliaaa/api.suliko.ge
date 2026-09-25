"""CSV exports: Excel-safe encoding, formula injection, labels, profit gating.

The list endpoints the exports page through are covered by their own tests;
here the list call is replaced so only the export layer is under test.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from suliko.api.v1 import exports
from suliko.api.v1 import finances as finances_api
from suliko.api.v1 import orders as orders_api
from suliko.api.v1._shared import PageMeta
from suliko.domain.plans import TenantPlan, effective_permissions
from suliko.domain.statuses import STATUS_DEFINITIONS
from suliko.models.directory import ClientType
from suliko.models.finance import PaymentMethod
from suliko.models.order import Urgency
from suliko.models.user import Role
from suliko.security.permissions import Permission
from suliko.security.sessions import AuthenticatedSession


def _session(role: Role = Role.OWNER) -> AuthenticatedSession:
    return AuthenticatedSession(
        session_id=1,
        user_id=1,
        username="u",
        full_name="U",
        email="u@acme.ge",
        role=role,
        tenant_id=1,
        tenant_slug="acme",
        tenant_name="Acme",
        plan=TenantPlan.BUREAU,
        onboarding_required=False,
        must_change_password=False,
        has_mfa=False,
        permissions=effective_permissions(role, TenantPlan.BUREAU),
        mfa_satisfied_at=None,
        impersonated_by_user_id=None,
    )


def test_formula_cells_are_defused_and_numbers_are_not() -> None:
    assert exports.safe_cell("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert exports.safe_cell("+995 555") == "'+995 555"
    assert exports.safe_cell("@SUM(A1)") == "'@SUM(A1)"
    assert exports.safe_cell("ნინო") == "ნინო"
    assert exports.safe_cell(Decimal("-5.00")) == Decimal("-5.00")
    assert exports.safe_cell(None) == ""
    assert exports.safe_cell(date(2026, 9, 25)) == "2026-09-25"


def test_csv_has_a_bom_so_excel_reads_georgian() -> None:
    response = exports.csv_response("x.csv", ["სახელი"], [["ნინო"]])
    body = bytes(response.body)
    assert body.startswith("﻿".encode())
    assert "სახელი\r\nნინო\r\n" in body.decode("utf-8")
    assert response.headers["content-disposition"] == 'attachment; filename="x.csv"'
    assert response.headers["cache-control"] == "private, no-store"


def test_every_status_and_enum_has_a_georgian_label() -> None:
    for status in STATUS_DEFINITIONS:
        assert status.replace(" ", "_") in exports.KA_STATUS, status
    assert set(exports.KA_METHOD) == {m.value for m in PaymentMethod}
    assert set(exports.KA_CATEGORY) == set(finances_api.EXPENSE_CATEGORIES)
    assert set(exports.KA_URGENCY) == {u.value for u in Urgency}
    assert set(exports.KA_CLIENT_TYPE) == {c.value for c in ClientType}


def _order(**overrides: Any) -> orders_api.OrderSummary:
    values: dict[str, Any] = {
        "id": 7,
        "order_date": date(2026, 9, 1),
        "due_date": date(2026, 9, 5),
        "client_id": 1,
        "client_name": "=cmd",
        "client_type": ClientType.B2C,
        "status": "being translated",
        "status_label": "Being Translated",
        "urgency": Urgency.STANDARD,
        "document_count": 2,
        "page_count": 3,
        "total": Decimal("60.00"),
        "paid": Decimal("20.00"),
        "profit": Decimal("25.00"),
        "paid_state": "partial",
        "is_overdue": True,
        "translator_names": ["Nino"],
        "language_pairs": ["ka→en"],
    }
    values.update(overrides)
    return orders_api.OrderSummary(**values)


@pytest.fixture
def one_order_page(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_list_orders(**kwargs: Any) -> orders_api.OrderPage:
        calls.append(kwargs)
        show = orders_api.shows_costs(kwargs["session"])
        order = _order() if show else _order(profit=None)
        return orders_api.OrderPage(items=[order], meta=PageMeta(total=1, limit=200, offset=0))

    monkeypatch.setattr(orders_api, "list_orders", fake_list_orders)
    return calls


async def _export(session: AuthenticatedSession, lang: exports.Lang = "ka") -> str:
    response = await exports.export_orders(db=None, session=session, lang=lang)  # type: ignore[arg-type]
    return bytes(response.body).decode("utf-8-sig")


async def test_orders_export_matches_the_list_and_translates(
    one_order_page: list[dict[str, Any]],
) -> None:
    text = await _export(_session())
    header, row = text.strip().split("\r\n")
    assert header.endswith(",მოგება")
    assert "'=cmd" in row  # defused
    assert "ითარგმნება" in row  # status in Georgian
    assert "ka→en" in row and "Nino" in row
    assert ",40.00," in row  # balance = total - paid
    assert row.endswith(",25.00")
    # The filters reach the list call unchanged, one page at a time.
    assert one_order_page[0]["limit"] == exports.PAGE
    assert one_order_page[0]["offset"] == 0


async def test_orders_export_has_no_profit_column_without_reports_profit(
    one_order_page: list[dict[str, Any]],
) -> None:
    staff = _session(Role.STAFF)
    assert Permission.REPORTS_PROFIT not in staff.permissions
    text = await _export(staff, lang="en")
    header, row = text.strip().split("\r\n")
    assert "Profit" not in header
    assert header.split(",")[-1] == "Overdue"
    assert row.endswith(",yes")

