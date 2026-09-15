"""The rules that stop money being recorded wrongly.

Everything here is pure validation — no database — because these are the
checks that run before anything is written, and they are the ones that decide
whether a ledger stays consistent.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticError

from suliko.api.v1.finances import (
    EXPENSE_CATEGORIES,
    Allocation,
    ExpenseIn,
    NotaryPayoutIn,
    PaymentIn,
    TranslatorPayoutIn,
    _money,
)
from suliko.models.finance import PaymentMethod

TODAY = date(2026, 9, 15)


# ── Null-safe aggregates ────────────────────────────────────────────────────


def test_money_turns_a_null_sum_into_zero() -> None:
    """`SUM` over no rows is NULL, and that NULL poisons every subtraction it
    reaches — an empty month would report a null balance, not a zero one."""
    assert _money(None) == Decimal(0)
    assert _money(0) == Decimal(0)
    assert _money(Decimal("12.34")) == Decimal("12.34")


def test_money_does_not_go_through_float() -> None:
    """A cent lost per row is a cent lost per row."""
    assert _money("0.1") + _money("0.2") == Decimal("0.3")


# ── Allocations cannot exceed the payment ───────────────────────────────────


def test_allocations_may_not_exceed_the_payment() -> None:
    """Otherwise a 100 GEL transfer could settle 300 GEL of invoices and the
    receivables figure would quietly under-report what is owed."""
    with pytest.raises(PydanticError, match="but the payment is only"):
        PaymentIn(
            client_id=1,
            amount=Decimal("100.00"),
            payment_date=TODAY,
            allocations=[
                Allocation(order_id=1, amount_allocated=Decimal("60.00")),
                Allocation(order_id=2, amount_allocated=Decimal("60.00")),
            ],
        )


def test_allocations_may_be_less_than_the_payment() -> None:
    """A client can pay ahead; the remainder shows as unallocated."""
    payment = PaymentIn(
        client_id=1,
        amount=Decimal("100.00"),
        payment_date=TODAY,
        allocations=[Allocation(order_id=1, amount_allocated=Decimal("40.00"))],
    )
    allocated = sum(a.amount_allocated for a in payment.allocations)
    assert payment.amount - allocated == Decimal("60.00")


def test_the_same_order_cannot_appear_twice() -> None:
    """Two rows against one order is not an error the database would catch,
    and it makes the order look more settled than it is."""
    with pytest.raises(PydanticError, match="appears twice"):
        PaymentIn(
            client_id=1,
            amount=Decimal("100.00"),
            payment_date=TODAY,
            allocations=[
                Allocation(order_id=7, amount_allocated=Decimal("10.00")),
                Allocation(order_id=7, amount_allocated=Decimal("20.00")),
            ],
        )


def test_a_payment_of_zero_is_rejected() -> None:
    with pytest.raises(PydanticError):
        PaymentIn(client_id=1, amount=Decimal("0"), payment_date=TODAY)


def test_a_negative_payment_is_rejected() -> None:
    """A negative payment is a refund, which is a different ledger and a
    different permission."""
    with pytest.raises(PydanticError):
        PaymentIn(client_id=1, amount=Decimal("-50.00"), payment_date=TODAY)


def test_unknown_payment_fields_are_rejected() -> None:
    """Mass-assignment defence: `tenant_id` in the body gets a 422, not a
    silent no-op that looks like it worked."""
    with pytest.raises(PydanticError):
        PaymentIn.model_validate(
            {
                "client_id": 1,
                "amount": "10.00",
                "payment_date": "2026-09-15",
                "tenant_id": 9999,
            }
        )


# ── Payouts ─────────────────────────────────────────────────────────────────


def test_translator_payout_allocations_are_bounded_too() -> None:
    with pytest.raises(PydanticError, match="but the payout is only"):
        TranslatorPayoutIn(
            translator_id=1,
            amount=Decimal("50.00"),
            payment_date=TODAY,
            allocations=[Allocation(order_id=1, amount_allocated=Decimal("80.00"))],
        )


def test_notary_payout_lists_must_line_up() -> None:
    """`document_ids` and `amounts` are parallel arrays. Mismatched lengths
    would zip short and silently drop allocations."""
    with pytest.raises(PydanticError, match="same length"):
        NotaryPayoutIn(
            amount=Decimal("30.00"),
            payment_date=TODAY,
            document_ids=[1, 2, 3],
            amounts=[Decimal("10.00")],
        )


def test_notary_payout_allocations_are_bounded() -> None:
    with pytest.raises(PydanticError, match="but the payout is only"):
        NotaryPayoutIn(
            amount=Decimal("10.00"),
            payment_date=TODAY,
            document_ids=[1, 2],
            amounts=[Decimal("6.00"), Decimal("6.00")],
        )


def test_a_notary_payout_needs_no_allocations() -> None:
    """The unattributed case: money paid to a notary office with no document
    breakdown recorded yet."""
    payout = NotaryPayoutIn(amount=Decimal("25.00"), payment_date=TODAY)
    assert payout.document_ids == []
    assert payout.method is PaymentMethod.BANK_TRANSFER


# ── Expenses ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("category", EXPENSE_CATEGORIES)
def test_every_listed_category_is_accepted(category: str) -> None:
    expense = ExpenseIn(
        expense_date=TODAY, category=category, description="x", amount=Decimal("1.00")
    )
    assert expense.category == category


def test_an_unlisted_category_is_rejected() -> None:
    """Free-text categories make the cost breakdown chart meaningless — every
    typo becomes its own slice."""
    with pytest.raises(PydanticError, match="category must be one of"):
        ExpenseIn(
            expense_date=TODAY,
            category="Courier",  # capitalised: not the stored value
            description="x",
            amount=Decimal("1.00"),
        )


def test_an_expense_with_no_order_is_a_general_expense() -> None:
    """Null `order_id` is meaningful, not missing data: it means the cost
    belongs to the company, not to any job, so no order's profit is reduced."""
    expense = ExpenseIn(
        expense_date=TODAY, category="office", description="Rent", amount=Decimal("900.00")
    )
    assert expense.order_id is None
