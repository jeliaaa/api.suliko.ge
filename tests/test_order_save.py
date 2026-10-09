"""`POST /orders/{id}/save` — the edit screen's one-transaction save.

Order routes need PostgreSQL (see [no-local-database]), so the ordering rules
are pinned by source inspection, as in test_email_verification.py; the
payload rules are plain pydantic and tested directly.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError as PydanticValidationError

from suliko.api.v1 import orders
from suliko.api.v1.orders import OrderDocumentChange, OrderSave


def _source() -> str:
    return inspect.getsource(orders.save_order)


def test_the_header_is_applied_before_any_document() -> None:
    source = _source()
    assert source.index("update_order(") < source.index("add_order_document(")


def test_adds_run_before_removals() -> None:
    """Replacing an order's only document must not trip the one-document rule."""
    source = _source()
    assert source.index("add_order_document(") < source.index("delete_order_document(")


def test_each_step_reuses_the_single_document_handler() -> None:
    source = _source()
    for handler in ("update_order_document(", "add_order_document(", "delete_order_document("):
        assert handler in source


def test_a_change_never_passes_its_id_as_a_field() -> None:
    assert 'exclude={"id"}' in _source()


def test_changing_and_removing_the_same_document_is_refused() -> None:
    assert "kept & set(payload.remove)" in _source()


def test_a_change_carries_only_what_was_sent() -> None:
    change = OrderDocumentChange(id=7, page_count=3)
    assert change.model_dump(exclude_unset=True, exclude={"id"}) == {"page_count": 3}


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(PydanticValidationError):
        OrderSave.model_validate({"remove": [1], "surprise": True})


# ── Moving an order to another client ──────────────────────────────────────


def _update_source() -> str:
    return inspect.getsource(orders.update_order)


def test_an_order_can_name_another_client() -> None:
    from suliko.api.v1.orders import OrderUpdate

    assert OrderUpdate.model_validate({"client_id": 5}).model_dump(exclude_unset=True) == {
        "client_id": 5
    }


def test_the_client_cannot_be_emptied() -> None:
    assert '"client_id", "order_date"' in _update_source()


def test_the_new_client_must_exist() -> None:
    source = _update_source()
    assert 'db.get(Client, changes["client_id"])' in source


def test_a_paid_order_keeps_its_client() -> None:
    """A payment settles its own client's orders, never someone else's."""
    source = _update_source()
    check = source.index("ClientPaymentAllocation.order_id == order.id")
    assert source.index("ConflictError(", check) > check
    # Checked before anything about the order is written.
    assert check < source.index("setattr(order, field, value)")


def test_naming_the_same_client_is_not_a_change() -> None:
    assert 'if changes.get("client_id") == order.client_id:' in _update_source()
