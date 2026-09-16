"""Whether a tenant may be used at all, and when that is checked.

Three things decide whether a sign-in works for a bureau other than the first
one, and each of them used to be wrong in a way that only shows up with more
than one tenant on the box.

`resolve_session` is checked by reading its source rather than by running it:
``user_sessions`` carries an ``INET`` column, so the table cannot be created
on SQLite, and there is no Postgres in this suite. Crude, but it fails loudly
the moment the check is deleted — the same approach `test_user_management.py`
takes for the guards it cannot execute.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from suliko.models.tenant import Tenant, TenantStatus

SRC = Path(__file__).resolve().parents[1] / "src" / "suliko"


# ── Which statuses may be used ──────────────────────────────────────────────


@pytest.mark.parametrize("status", [TenantStatus.ACTIVE, TenantStatus.TRIAL])
def test_active_and_trial_tenants_are_usable(status: TenantStatus) -> None:
    """A trial is a paying-customer-to-be, not a disabled account."""
    assert Tenant(slug="acme", display_name="Acme", status=status).is_usable is True


def test_a_suspended_tenant_is_not_usable() -> None:
    assert (
        Tenant(slug="acme", display_name="Acme", status=TenantStatus.SUSPENDED).is_usable is False
    )


def test_every_status_is_decided() -> None:
    """A new TenantStatus must not default to "usable" by omission."""
    for status in TenantStatus:
        usable = Tenant(slug="x", display_name="X", status=status).is_usable
        assert isinstance(usable, bool)
        assert usable == (status in (TenantStatus.ACTIVE, TenantStatus.TRIAL))


# ── Where that is enforced ──────────────────────────────────────────────────


def test_session_resolution_rejects_a_suspended_tenant() -> None:
    """Suspension has to bite on the NEXT request, not whenever the session
    happens to expire.

    Login already refuses a suspended tenant, so without this check suspending
    a bureau left everyone who was already signed in working for up to the
    absolute session timeout — which is most of a working day.
    """
    from suliko.security.sessions import resolve_session

    source = inspect.getsource(resolve_session)
    assert "tenant.is_usable" in source, (
        "resolve_session no longer checks tenant.is_usable. Suspending a "
        "tenant would stop taking effect until their sessions expire."
    )


def test_login_binds_the_tenant_guc_before_writing() -> None:
    """Login opens its session before it knows the tenant, so nothing has set
    ``suliko.tenant_id`` — the GUC the row-level-security policies read.

    It then writes tenant-scoped rows (the session row, and the login
    timestamp). Without the bind those inserts are rejected by the
    ``tenant_isolation`` policy the moment the application stops connecting as
    a PostgreSQL superuser, which is the intended end state.
    """
    source = (SRC / "api" / "v1" / "auth.py").read_text(encoding="utf-8")

    login = source.split("async def login(")[1].split("\n@router")[0]
    assert "bind_tenant_guc" in login, (
        "login() no longer binds the tenant GUC. Its tenant-scoped inserts "
        "will fail under row-level security."
    )


def test_issuing_a_reset_token_binds_the_tenant_guc() -> None:
    """Same reasoning as login: the forgot-password endpoint finds the user
    before it knows the tenant, then writes a tenant-scoped token row."""
    from suliko.security.reset_tokens import issue

    assert "bind_tenant_guc" in inspect.getsource(issue)
