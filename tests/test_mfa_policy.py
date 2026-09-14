"""The MFA_ENFORCED switch.

`MFA_ENFORCED=false` is a temporary measure while the enrolment screen is
built. These tests pin the behaviour in BOTH positions, so turning it back on
is a one-line change with no surprises — and so nobody can quietly weaken the
enforced path while the switch happens to be off.

The matrix is the whole point. `login()` derives three booleans from two
inputs (is a factor enrolled, does the role demand one) plus the switch, and
getting any cell wrong either locks someone out or lets them in early.
"""

from __future__ import annotations

import pytest

from suliko.models.user import Role
from suliko.security.permissions import requires_mfa


def decide(*, enforced: bool, has_mfa: bool, role: Role) -> dict[str, bool]:
    """Mirror of the decision in `api/v1/auth.py::login`.

    Duplicated rather than imported because the real one is entangled with a
    database session and a request. `test_mirror_matches_source` below guards
    against the two drifting.
    """
    must_have_mfa = enforced and requires_mfa(role)
    challenge_owed = enforced and has_mfa
    enrolment_required = must_have_mfa and not has_mfa

    return {
        "mfa_satisfied": not (challenge_owed or enrolment_required),
        "mfa_required": challenge_owed,
        "enrolment_required": enrolment_required,
    }


# ── MFA enforced (the normal, intended state) ───────────────────────────────


def test_enforced_superuser_with_factor_is_challenged() -> None:
    d = decide(enforced=True, has_mfa=True, role=Role.SUPERUSER)
    assert d == {"mfa_satisfied": False, "mfa_required": True, "enrolment_required": False}


def test_enforced_superuser_without_factor_must_enrol() -> None:
    """Fails closed. A privileged account missing its factor gets neither a
    satisfied session nor a challenge it cannot answer."""
    d = decide(enforced=True, has_mfa=False, role=Role.SUPERUSER)
    assert d == {"mfa_satisfied": False, "mfa_required": False, "enrolment_required": True}


def test_enforced_staff_with_voluntary_factor_is_challenged() -> None:
    """The bug this originally fixed: a staff user who chose to enrol TOTP was
    let in on their password alone, silently ignoring the factor they added."""
    d = decide(enforced=True, has_mfa=True, role=Role.STAFF)
    assert d == {"mfa_satisfied": False, "mfa_required": True, "enrolment_required": False}


def test_enforced_staff_without_factor_signs_straight_in() -> None:
    d = decide(enforced=True, has_mfa=False, role=Role.STAFF)
    assert d == {"mfa_satisfied": True, "mfa_required": False, "enrolment_required": False}


# ── MFA disabled (the temporary state) ──────────────────────────────────────


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("has_mfa", [True, False])
def test_disabled_always_signs_straight_in(role: Role, has_mfa: bool) -> None:
    """Every role, enrolled or not, completes on the password alone."""
    d = decide(enforced=False, has_mfa=has_mfa, role=role)
    assert d == {"mfa_satisfied": True, "mfa_required": False, "enrolment_required": False}


def test_disabled_does_not_challenge_an_enrolled_superuser() -> None:
    """The specific case that blocks the current deployment: the CLI enrolled
    a factor for the superuser, and with MFA off that must not strand them on
    a challenge screen."""
    assert decide(enforced=False, has_mfa=True, role=Role.SUPERUSER)["mfa_required"] is False


def test_disabled_never_demands_enrolment() -> None:
    """Otherwise the frontend's fail-closed branch would lock out every
    privileged account that has no factor."""
    for role in Role:
        assert decide(enforced=False, has_mfa=False, role=role)["enrolment_required"] is False


# ── Guards ──────────────────────────────────────────────────────────────────


def test_default_is_enforced() -> None:
    """A fresh deployment must require MFA. Disabling it has to be a
    deliberate, written-down act, never a default.

    `_env_file=None` is essential: without it this reads whatever the local
    .env says, so on a machine that has MFA switched off the test would assert
    that switching it off is the default — and pass.
    """
    from suliko.config import Settings

    assert Settings(_env_file=None).mfa_enforced is True


def test_mirror_matches_source() -> None:
    """`decide()` above must stay in step with the real implementation.

    Compares the source text rather than running it, because the real function
    needs a database. Crude, but it fails loudly the moment someone edits the
    logic in one place and not the other.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "suliko" / "api" / "v1" / "auth.py"
    ).read_text(encoding="utf-8")

    for expression in (
        "must_have_mfa = enforced and requires_mfa(user.role)",
        "challenge_owed = enforced and has_mfa",
        "enrolment_required = must_have_mfa and not has_mfa",
        "mfa_satisfied=not (challenge_owed or enrolment_required)",
        "mfa_required=challenge_owed",
    ):
        assert expression in source, (
            f"auth.py no longer contains {expression!r}. The mirror in "
            "tests/test_mfa_policy.py::decide is now out of date — update both."
        )


# ── The request gate ────────────────────────────────────────────────────────


async def _gate(*, enforced: bool, mfa_satisfied_at: object) -> bool:
    """Call get_authenticated_session directly. True when it lets the request through."""
    from datetime import UTC, datetime

    from suliko.api.deps import get_authenticated_session
    from suliko.config import get_settings
    from suliko.core.errors import MfaRequiredError
    from suliko.security.permissions import permissions_for_role
    from suliko.security.sessions import AuthenticatedSession

    settings = get_settings()
    original = settings.mfa_enforced
    object.__setattr__(settings, "mfa_enforced", enforced)
    try:
        session = AuthenticatedSession(
            session_id=1,
            user_id=1,
            username="u",
            full_name="U",
            email="u@example.com",
            role=Role.SUPERUSER,
            tenant_id=1,
            permissions=permissions_for_role(Role.SUPERUSER),
            mfa_satisfied_at=(datetime.now(UTC) if mfa_satisfied_at else None),
            impersonated_by_user_id=None,
        )
        try:
            await get_authenticated_session(session)
            return True
        except MfaRequiredError:
            return False
    finally:
        object.__setattr__(settings, "mfa_enforced", original)


async def test_gate_blocks_a_pending_session_when_enforced() -> None:
    assert await _gate(enforced=True, mfa_satisfied_at=None) is False


async def test_gate_allows_a_satisfied_session_when_enforced() -> None:
    assert await _gate(enforced=True, mfa_satisfied_at=True) is True


async def test_gate_allows_a_pending_session_when_disabled() -> None:
    """Sessions issued BEFORE the switch was flipped carry a null
    mfa_satisfied_at. Without this check they would be stuck forever on a
    challenge the server no longer issues."""
    assert await _gate(enforced=False, mfa_satisfied_at=None) is True


def test_session_endpoint_agrees_with_the_gate() -> None:
    """`/auth/session` must not report a requirement the gate is not enforcing.

    The frontend routes on `mfa_satisfied` from this endpoint. If it said
    false while the gate let requests through, the user would be bounced to a
    challenge screen forever — the API would happily serve them, and the
    frontend would never let them ask.

    This bites specifically on sessions created BEFORE MFA was switched off,
    which carry a null mfa_satisfied_at.
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "suliko" / "api" / "v1" / "auth.py"
    ).read_text(encoding="utf-8")

    assert "or not get_settings().mfa_enforced" in source, (
        "SessionInfo.mfa_satisfied no longer accounts for MFA_ENFORCED. It must "
        "mirror deps.get_authenticated_session, or stale sessions strand the "
        "frontend on the two-factor page."
    )
