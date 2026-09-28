"""Email verification: where the wiring lives, checked by reading the source.

`signup`, `verify_email`, `resend_verification_email` and `reset_password`
each open their own session via `get_sessionmaker()` rather than taking one
as a dependency, and `users.create_user` writes a row whose table
(`users`) is portable but whose surrounding endpoint pulls in the session
machinery too. None of that is reachable without a real Postgres — there is
none in this suite — so what is pinned here is the same thing
`test_tenant_access.py` and `test_user_management.py` already pin for their
own unrunnable endpoints: the exact line that does the right thing is still
in the source. Crude, but it fails loudly the moment someone deletes it.

Everything that IS runnable without a database — the token purpose, the
email body, the session field's default — has its own test elsewhere:
`test_password_reset.py`, `test_mail.py`, `test_tenant_access.py`.
"""

from __future__ import annotations

import inspect

from suliko.api.v1 import auth, platform, users


def test_signup_issues_a_verification_token_not_a_reset_one() -> None:
    """A stray default would silently mint the WRONG purpose — a token that
    would then let `POST /auth/verify-email` be spent as a password reset,
    or vice versa. `reset_tokens.EMAIL_VERIFICATION` must be explicit."""
    source = inspect.getsource(auth.signup)
    assert "reset_tokens.issue" in source
    assert "purpose=reset_tokens.EMAIL_VERIFICATION" in source


def test_signup_sends_the_verification_mail_after_the_commit() -> None:
    """Before the commit, a fast mail relay can deliver a link whose row is
    not durable yet — the same class of bug `forgot_password` already avoids
    (see its own docstring)."""
    source = inspect.getsource(auth.signup)
    commit_at = source.index("await db.commit()")
    send_at = source.index("mail.send")
    assert send_at > commit_at


def test_verify_email_consumes_the_verification_purpose() -> None:
    source = inspect.getsource(auth.verify_email)
    assert "purpose=reset_tokens.EMAIL_VERIFICATION" in source
    # Marks it, doesn't just check it — the whole point of the endpoint.
    assert "user.email_verified_at = " in source or "email_verified_at =" in source


def test_resend_is_rate_limited_per_account_before_it_sends() -> None:
    """Authenticated, so there is no third party to spam — but "click resend
    a hundred times" must still be bounded, and the limiter has to run BEFORE
    `reset_tokens.issue` mints anything, or the check is decorative."""
    source = inspect.getsource(auth.resend_verification_email)
    check_at = source.index("check_email_verification_resend")
    issue_at = source.index("reset_tokens.issue")
    assert check_at < issue_at


def test_resend_does_nothing_once_already_verified() -> None:
    source = inspect.getsource(auth.resend_verification_email)
    assert "email_verified_at is not None" in source


def test_reset_password_also_marks_the_email_verified() -> None:
    """The one path an invited user completes instead of ever getting a
    verification email of their own — see the comment in `reset_password`
    for why this has to be here and not only in `verify_email`."""
    source = inspect.getsource(auth.reset_password)
    assert "user.email_verified_at is None" in source
    assert "user.email_verified_at = datetime.now(UTC)" in source


def test_the_invite_flow_does_not_pre_mark_verified() -> None:
    """The opposite check: an invited user has not proven anything about
    their mailbox until they click the link, so `invite_user`'s own `User(`
    construction must NOT set `email_verified_at` — `reset_password` is where
    that happens, once they act on it."""
    source = inspect.getsource(users.invite_user)
    construction = source[source.index("row = User(") : source.index("db.add(row)")]
    assert "email_verified_at" not in construction


def test_admin_created_accounts_start_verified() -> None:
    """No email loop happens for these three — an admin, a platform operator,
    or whoever has shell access on the box typed the address directly — so
    there is nothing to nag them to confirm."""
    for source in (
        inspect.getsource(users.create_user),
        inspect.getsource(platform.create_tenant_user),
    ):
        assert "email_verified_at=datetime.now(UTC)" in source
