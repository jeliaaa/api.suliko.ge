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
from suliko.security.permissions import Permission, requires_mfa


def decide(
    *, enforced: bool, has_mfa: bool, role: Role, require_enrolment: bool = True
) -> dict[str, bool]:
    """Mirror of the decision in `api/v1/auth.py::login`.

    Duplicated rather than imported because the real one is entangled with a
    database session and a request. `test_mirror_matches_source` below guards
    against the two drifting.

    ``require_enrolment`` defaults to True here and False in `Settings`. The
    default is deliberately the opposite way round: these tests are about
    pinning the STRICT policy, and every case that relaxes it says so.
    """
    must_have_mfa = enforced and requires_mfa(role)
    challenge_owed = enforced and has_mfa
    enrolment_required = require_enrolment and must_have_mfa and not has_mfa

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


# ── Enrolment not required (the shipping default) ───────────────────────────
#
# MFA_ENFORCED stays on, so anyone holding a factor is still challenged for
# it. What is relaxed is the demand that a privileged role HAVE one — because
# there is no enrolment screen, so failing that closed locks out the owner and
# every admin of every tenant with no action available to them.


@pytest.mark.parametrize("role", [Role.SUPERUSER, Role.OWNER, Role.ADMIN])
def test_a_privileged_account_without_a_factor_can_sign_in(role: Role) -> None:
    """The lockout this flag exists to remove.

    Every one of these roles is in MFA_REQUIRED_ROLES, and a freshly created
    tenant's owner has no factor. Before this, they could not reach their own
    product at all.
    """
    d = decide(enforced=True, has_mfa=False, role=role, require_enrolment=False)
    assert d == {"mfa_satisfied": True, "mfa_required": False, "enrolment_required": False}


@pytest.mark.parametrize("role", list(Role))
def test_an_enrolled_factor_is_still_challenged(role: Role) -> None:
    """The half that must NOT be relaxed.

    Relaxing enrolment must not quietly stop honouring factors people already
    have — that would silently downgrade every account the CLI has enrolled.
    """
    d = decide(enforced=True, has_mfa=True, role=role, require_enrolment=False)
    assert d["mfa_required"] is True
    assert d["mfa_satisfied"] is False


def test_relaxing_enrolment_never_demands_enrolment() -> None:
    """The frontend's fail-closed branch must be unreachable in this state."""
    for role in Role:
        d = decide(enforced=True, has_mfa=False, role=role, require_enrolment=False)
        assert d["enrolment_required"] is False


def test_the_strict_policy_is_one_flag_away() -> None:
    """Turning MFA_REQUIRE_ENROLMENT on restores the original behaviour
    exactly, so shipping the enrolment screen is a config change."""
    d = decide(enforced=True, has_mfa=False, role=Role.OWNER, require_enrolment=True)
    assert d == {"mfa_satisfied": False, "mfa_required": False, "enrolment_required": True}


# ── Guards ──────────────────────────────────────────────────────────────────


def test_enrolment_is_not_required_by_default() -> None:
    """The counterpart to `test_default_is_enforced`.

    A default that locks out the owner of every new tenant is not a safe
    default, it is an outage. This one has to be flipped deliberately, the
    same way MFA_ENFORCED does — and startup warns on every boot until it is.
    """
    from suliko.config import Settings

    assert Settings(_env_file=None).mfa_require_enrolment is False


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
        "enrolment_required = require_enrolment and must_have_mfa and not has_mfa",
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
    from suliko.domain.plans import TenantPlan
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
            tenant_slug="acme",
            tenant_name="Acme Translations",
            plan=TenantPlan.BUREAU,
            onboarding_required=False,
            must_change_password=False,
            has_mfa=False,
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


# ── Step-up ─────────────────────────────────────────────────────────────────
#
# `STEP_UP_PERMISSIONS` demands a code verified within the last few minutes
# before Settings, Users, the plan choice, API keys or an outbound transfer.
# That is a real control for someone who HAS a factor. For someone who does
# not, it is a permanent refusal five minutes after every login, satisfiable
# by nothing — which is what these pin.


async def _step_up(
    *, permission: object, minutes_since_login: int, has_mfa: bool, enforced: bool = True
) -> bool:
    """True when the request is allowed through."""
    from datetime import UTC, datetime, timedelta

    from suliko.api.deps import require
    from suliko.config import get_settings
    from suliko.core.errors import StepUpRequiredError
    from suliko.domain.plans import TenantPlan
    from suliko.security.permissions import permissions_for_role
    from suliko.security.sessions import AuthenticatedSession

    settings = get_settings()
    original = settings.mfa_enforced
    object.__setattr__(settings, "mfa_enforced", enforced)
    try:
        session = AuthenticatedSession(
            session_id=1,
            user_id=1,
            username="owner",
            full_name="Owner",
            email="owner@acme.test",
            role=Role.OWNER,
            tenant_id=1,
            tenant_slug="acme",
            tenant_name="Acme Translations",
            plan=TenantPlan.BUREAU,
            onboarding_required=False,
            must_change_password=False,
            has_mfa=has_mfa,
            permissions=permissions_for_role(Role.OWNER),
            # What login stamps: for a user with no factor, the moment they
            # signed in; for one with a factor, the moment they answered.
            mfa_satisfied_at=datetime.now(UTC) - timedelta(minutes=minutes_since_login),
            impersonated_by_user_id=None,
        )
        try:
            await require(permission)(session)  # type: ignore[arg-type]
            return True
        except StepUpRequiredError:
            return False
    finally:
        object.__setattr__(settings, "mfa_enforced", original)


@pytest.mark.parametrize(
    "permission",
    [
        Permission.SETTINGS_MANAGE,
        Permission.USERS_MANAGE,
        Permission.TENANT_MANAGE,
        Permission.APIKEYS_MANAGE,
    ],
)
async def test_a_user_with_no_factor_is_not_asked_to_step_up(permission: Permission) -> None:
    """The bug this fixes, and it was live with MFA switched off.

    Five minutes after login, an owner with no enrolled factor was refused
    Settings, the Users screen, the onboarding plan choice and API keys — and
    the only way to clear it was a TOTP code they did not have. Every one of
    those screens was simply unusable after the first few minutes.
    """
    assert await _step_up(permission=permission, minutes_since_login=60, has_mfa=False)


@pytest.mark.parametrize("enforced", [True, False])
async def test_the_switch_does_not_resurrect_the_dead_end(enforced: bool) -> None:
    """It bit in BOTH positions of MFA_ENFORCED, because login stamps
    `mfa_satisfied_at` either way."""
    assert await _step_up(
        permission=Permission.SETTINGS_MANAGE,
        minutes_since_login=60,
        has_mfa=False,
        enforced=enforced,
    )


async def test_an_enrolled_user_is_still_asked_to_step_up() -> None:
    """The half that must NOT be relaxed. Someone who can answer a challenge
    is still asked — that is the whole point of the control."""
    assert not await _step_up(
        permission=Permission.SETTINGS_MANAGE, minutes_since_login=60, has_mfa=True
    )


async def test_a_fresh_code_satisfies_step_up() -> None:
    assert await _step_up(
        permission=Permission.SETTINGS_MANAGE, minutes_since_login=1, has_mfa=True
    )


async def test_a_permission_outside_the_step_up_set_is_never_delayed() -> None:
    assert await _step_up(permission=Permission.ORDERS_WRITE, minutes_since_login=600, has_mfa=True)
