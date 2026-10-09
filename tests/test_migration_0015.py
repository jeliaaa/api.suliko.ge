"""Migration 0015 — a translator invite no longer needs an email address.

No database: the revision runs against the recorder, where the column looks
NOT NULL, so the change is recorded and compared with the model.
"""

from __future__ import annotations

from typing import Any

import pytest

from migration_recorder import RecordedOps, load_revision
from suliko.models import PortalAccountInvite

FILENAME = "0015_invite_without_email.py"


@pytest.fixture(scope="module")
def revision() -> tuple[Any, RecordedOps]:
    return load_revision(FILENAME)


def test_the_email_is_made_optional_and_the_model_agrees(
    revision: tuple[Any, RecordedOps],
) -> None:
    _, ops = revision
    assert ("portal_account_invites", "email", True) in ops.altered_columns
    assert PortalAccountInvite.__table__.c.email.nullable is True


def test_nothing_else_changes(revision: tuple[Any, RecordedOps]) -> None:
    module, ops = revision
    assert ops.altered_columns == [("portal_account_invites", "email", True)]
    assert not ops.tables and not ops.added_columns and not ops.unique_constraints
    assert module.NEW_TABLES == ()


def test_the_type_is_unchanged(revision: tuple[Any, RecordedOps]) -> None:
    module, _ = revision
    (table, column, column_type) = module.NOW_NULLABLE[0]
    assert str(column_type) == str(PortalAccountInvite.__table__.c[column].type)
    assert table == PortalAccountInvite.__tablename__


def test_the_unique_constraint_on_the_address_is_kept() -> None:
    """Two invites for one ADDRESS still collide; NULLs are distinct, so any
    number of addressless ones do not."""
    constraints = {c.name for c in PortalAccountInvite.__table__.constraints}
    assert "uq_portal_account_invites_tenant_kind_email" in constraints


def test_downgrade_does_not_put_the_requirement_back() -> None:
    module, _ = load_revision(FILENAME)
    recorder = RecordedOps()
    module.op = recorder
    module.downgrade()
    assert recorder.altered_columns == []
