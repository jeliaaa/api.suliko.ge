"""Signing up with an address that already has an account anywhere.

`signup` opens its own session, so the lookup itself is pinned by source
inspection (as in test_email_verification.py); how a holder is described is
a plain function and tested directly.
"""

from __future__ import annotations

import inspect

from suliko.api.v1 import auth
from suliko.api.v1.auth import EmailTakenError, _existing_account


def test_a_bureau_is_named() -> None:
    assert _existing_account("bureau", "Tbilisi Translations") == {
        "kind": "bureau",
        "name": "Tbilisi Translations",
    }


def test_a_freelancer_is_not_named() -> None:
    assert _existing_account("freelancer", "Nino Beridze") == {"kind": "freelancer"}


def test_an_unchosen_plan_counts_as_freelancer() -> None:
    assert _existing_account(None, "Nino Beridze") == {"kind": "freelancer"}


def test_the_error_is_a_409_the_frontend_can_tell_apart() -> None:
    error = EmailTakenError("taken", accounts=[{"kind": "freelancer"}])
    assert error.status_code == 409
    assert error.error_code == "email_taken"
    assert error.extra == {"accounts": [{"kind": "freelancer"}]}


def test_signup_looks_across_every_organisation_case_insensitively() -> None:
    source = inspect.getsource(auth.signup)
    lookup = source[source.index("holders = ") :]
    assert "func.lower(User.email) == email" in lookup
    before_lookup = source[: source.index("holders = ")]
    assert before_lookup.rstrip().endswith("with bypass_tenant_scope():")


def test_signup_checks_the_address_before_creating_anything() -> None:
    source = inspect.getsource(auth.signup)
    check = source.index("raise EmailTakenError")
    assert check < source.index("unique_slug(")
    assert check < source.index("Account(")
    assert check < source.index("Tenant(")
    assert check < source.index("User(")


def test_each_probe_spends_a_signup_attempt() -> None:
    source = inspect.getsource(auth.signup)
    assert source.index("limiter.record_signup(") < source.index("holders = ")
