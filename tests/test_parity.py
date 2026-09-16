"""Parity between the Python backend and the TypeScript frontend.

Three things are defined in both codebases and MUST agree:

  - the status registry (labels and colour tones)
  - the permission strings
  - the role -> permission bundles

They are duplicated on purpose: the frontend needs them to render without a
round trip, the backend needs them to enforce. Duplication is fine; silent
divergence is not. A status that exists only in Python renders as a grey
"Unknown" pill; a permission granted in TypeScript but not Python shows the
user a button that 403s.

These tests parse the TypeScript source directly. If the frontend is not
checked out next to this repo they skip rather than fail — CI for this repo
alone should not depend on a sibling checkout.
"""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path

import pytest

from suliko.domain.statuses import STATUS_DEFINITIONS
from suliko.models.user import Role
from suliko.security.permissions import ROLE_PERMISSIONS, Permission

FRONTEND = Path(__file__).resolve().parents[2] / "app.suliko.ge"
STATUSES_TS = FRONTEND / "src" / "shared" / "lib" / "statuses.ts"
PERMISSIONS_TS = FRONTEND / "src" / "shared" / "auth" / "permissions.ts"

requires_frontend = pytest.mark.skipif(
    not STATUSES_TS.exists() or not PERMISSIONS_TS.exists(),
    reason="frontend checkout not present next to this repo",
)


def _parse_ts_statuses(source: str) -> dict[str, tuple[str, str]]:
    """Extract ``"key": { label: "...", tone: "..." }`` pairs."""
    body = source.split("export const STATUS_DEFINITIONS", 1)[1]
    body = body.split("} as const", 1)[0]

    pattern = re.compile(
        r'"(?P<key>[^"]+)":\s*\{\s*label:\s*"(?P<label>[^"]*)",\s*tone:\s*"(?P<tone>[^"]*)"'
    )
    return {m["key"]: (m["label"], m["tone"]) for m in pattern.finditer(body)}


def _parse_ts_permissions(source: str) -> set[str]:
    body = source.split("export const PERMISSIONS = [", 1)[1].split("] as const", 1)[0]
    return set(re.findall(r'"([a-z_]+\.[a-z_]+)"', body))


def _parse_ts_role_bundle(source: str, name: str) -> set[str]:
    """Resolve one bundle, following its `...SPREAD` of a previous bundle."""
    match = re.search(rf"const {name}: Permission\[\] = \[(?P<body>.*?)\];", source, re.DOTALL)
    assert match, f"bundle {name} not found in permissions.ts"
    body = match["body"]

    permissions = set(re.findall(r'"([a-z_]+\.[a-z_]+)"', body))
    for inherited in re.findall(r"\.\.\.([A-Z_]+)", body):
        permissions |= _parse_ts_role_bundle(source, inherited)
    return permissions


@requires_frontend
def test_status_keys_match() -> None:
    ts = _parse_ts_statuses(STATUSES_TS.read_text(encoding="utf-8"))
    assert set(ts) == set(STATUS_DEFINITIONS), "status keys diverged between Python and TypeScript"


@requires_frontend
def test_status_labels_and_tones_match() -> None:
    ts = _parse_ts_statuses(STATUSES_TS.read_text(encoding="utf-8"))
    for key, definition in STATUS_DEFINITIONS.items():
        ts_label, ts_tone = ts[key]
        assert definition.label == ts_label, f"label mismatch for {key!r}"
        assert definition.tone.value == ts_tone, f"tone mismatch for {key!r}"


@requires_frontend
def test_permission_strings_match() -> None:
    ts = _parse_ts_permissions(PERMISSIONS_TS.read_text(encoding="utf-8"))
    py = {p.value for p in Permission}
    assert ts == py, f"only in TS: {sorted(ts - py)} | only in Python: {sorted(py - ts)}"


@requires_frontend
@pytest.mark.parametrize(
    ("role", "ts_bundle"),
    [
        (Role.STAFF, "STAFF"),
        (Role.MANAGER, "MANAGER"),
        (Role.ADMIN, "ADMIN"),
        (Role.OWNER, "OWNER"),
        (Role.SUPERUSER, "SUPERUSER"),
    ],
)
def test_role_bundles_match(role: Role, ts_bundle: str) -> None:
    source = PERMISSIONS_TS.read_text(encoding="utf-8")
    ts = _parse_ts_role_bundle(source, ts_bundle)
    py = {p.value for p in ROLE_PERMISSIONS[role]}
    assert ts == py, (
        f"{role.value}: only in TS: {sorted(ts - py)} | only in Python: {sorted(py - ts)}"
    )


# ── Invariants that hold regardless of the frontend ─────────────────────────


def test_roles_are_strictly_nested() -> None:
    """Each role must be a superset of the one below it.

    The hierarchy is documented as cumulative. If someone grants `manager` a
    permission without giving it to `admin`, an admin loses an ability their
    subordinate has — the kind of bug that is obvious stated this way and
    invisible in a diff.
    """
    order = [Role.STAFF, Role.MANAGER, Role.ADMIN, Role.OWNER, Role.SUPERUSER]
    for lower, higher in pairwise(order):
        missing = ROLE_PERMISSIONS[lower] - ROLE_PERMISSIONS[higher]
        assert not missing, f"{higher.value} is missing {sorted(p.value for p in missing)}"


def test_platform_permissions_are_superuser_only() -> None:
    platform = {p for p in Permission if p.value.startswith("platform.")}
    for role, granted in ROLE_PERMISSIONS.items():
        if role is Role.SUPERUSER:
            assert platform <= granted
        else:
            assert not (platform & granted), f"{role.value} must not hold platform permissions"


def test_staff_cannot_move_money() -> None:
    """The specific gap this model closes: in the PHP, staff and manager are
    functionally identical, including on the money screens."""
    staff = ROLE_PERMISSIONS[Role.STAFF]
    for permission in (
        Permission.FINANCE_RECORD_PAYMENT,
        Permission.FINANCE_REFUND,
        Permission.FINANCE_TRANSFER,
        Permission.FINANCE_READ,
        Permission.REPORTS_PROFIT,
    ):
        assert permission not in staff


def test_only_admin_and_above_can_transfer_funds() -> None:
    for role in (Role.STAFF, Role.MANAGER):
        assert Permission.FINANCE_TRANSFER not in ROLE_PERMISSIONS[role]
    for role in (Role.ADMIN, Role.OWNER, Role.SUPERUSER):
        assert Permission.FINANCE_TRANSFER in ROLE_PERMISSIONS[role]


# ── Plans ───────────────────────────────────────────────────────────────────

PLANS_TS = FRONTEND / "src" / "shared" / "auth" / "plans.ts"

requires_plans_ts = pytest.mark.skipif(
    not PLANS_TS.exists(), reason="frontend checkout not present next to this repo"
)


def _ts_string_array(source: str, name: str) -> list[str]:
    """Extract ``export const NAME = ["a", "b"] as const;``."""
    body = source.split(f"export const {name} = ", 1)[1].split("]", 1)[0]
    return re.findall(r'"([^"]+)"', body)


@requires_plans_ts
def test_the_plan_names_agree() -> None:
    """A plan the frontend knows and the backend does not is enforced as the
    default — silently, and as the WRONG plan."""
    from suliko.domain.plans import TenantPlan

    source = PLANS_TS.read_text(encoding="utf-8")
    assert set(_ts_string_array(source, "PLANS")) == {p.value for p in TenantPlan}


@requires_plans_ts
def test_the_feature_names_agree() -> None:
    from suliko.domain.plans import Feature

    source = PLANS_TS.read_text(encoding="utf-8")
    assert set(_ts_string_array(source, "FEATURES")) == {f.value for f in Feature}


@requires_plans_ts
def test_the_default_plan_agrees() -> None:
    """If these disagree, a tenant mid-onboarding sees one plan's tabs and is
    refused by the other's."""
    from suliko.domain.plans import DEFAULT_PLAN

    source = PLANS_TS.read_text(encoding="utf-8")
    match = re.search(r'export const DEFAULT_PLAN: Plan = "([^"]+)"', source)
    assert match, "plans.ts no longer declares DEFAULT_PLAN"
    assert match.group(1) == DEFAULT_PLAN.value


@requires_plans_ts
def test_the_feature_bundles_agree() -> None:
    """Notifications is the one screen gated by plan rather than permission,
    so the two lists are the only thing keeping the tab and the endpoint in
    step."""
    from suliko.domain.plans import PLAN_FEATURES, TenantPlan

    source = PLANS_TS.read_text(encoding="utf-8")
    for plan in TenantPlan:
        name = f"{plan.value.upper()}_FEATURES"
        # Split on `=` before `]`: the declaration carries a `Feature[]` type
        # annotation, whose own bracket comes first.
        body = source.split(f"const {name}", 1)[1].split("=", 1)[1].split("]", 1)[0]
        declared = set(re.findall(r'"([^"]+)"', body))
        assert declared == {f.value for f in PLAN_FEATURES[plan]}, f"{plan.value}: features differ"
