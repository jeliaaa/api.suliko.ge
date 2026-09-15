"""User-management guard rails.

`users.manage` is the permission closest to full control of a tenant, so the
rules that stop it becoming full control are worth pinning explicitly. Each of
these describes a way someone with that permission could otherwise escalate.
"""

from __future__ import annotations

import pytest

from suliko.api.v1.users import RANK, _guard_assignable
from suliko.core.errors import ValidationError
from suliko.models.user import Role

# ── Role seniority ──────────────────────────────────────────────────────────


def test_rank_covers_every_role() -> None:
    """A role missing from RANK would raise a KeyError inside the guard —
    failing closed, but with a 500 rather than a clear message."""
    assert set(RANK) == set(Role)


def test_rank_is_strictly_ordered() -> None:
    order = [Role.STAFF, Role.MANAGER, Role.ADMIN, Role.OWNER, Role.SUPERUSER]
    ranks = [RANK[r] for r in order]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks), "two roles share a rank, so neither outranks the other"


# ── Cannot assign above your own role ───────────────────────────────────────


@pytest.mark.parametrize(
    ("actor", "target"),
    [
        (Role.ADMIN, Role.OWNER),
        (Role.MANAGER, Role.ADMIN),
        (Role.MANAGER, Role.OWNER),
        (Role.STAFF, Role.MANAGER),
    ],
)
def test_cannot_assign_a_higher_role(actor: Role, target: Role) -> None:
    """Otherwise users.manage is a one-step path to owner and the permission
    bundles stop meaning anything."""
    with pytest.raises(ValidationError, match="above your own"):
        _guard_assignable(actor, target)


@pytest.mark.parametrize(
    ("actor", "target"),
    [
        (Role.OWNER, Role.ADMIN),
        (Role.OWNER, Role.OWNER),
        (Role.ADMIN, Role.ADMIN),
        (Role.ADMIN, Role.STAFF),
        (Role.MANAGER, Role.STAFF),
    ],
)
def test_can_assign_at_or_below_own_role(actor: Role, target: Role) -> None:
    _guard_assignable(actor, target)


# ── Superuser is never assignable over HTTP ─────────────────────────────────


@pytest.mark.parametrize("actor", list(Role))
def test_superuser_cannot_be_granted_through_the_api(actor: Role) -> None:
    """Not even by another superuser.

    A platform-wide account must require filesystem access to the server, so
    that compromising one tenant's admin session can never mint one. The CLI
    is the only path.
    """
    with pytest.raises(ValidationError, match="server console"):
        _guard_assignable(actor, Role.SUPERUSER)


# ── The rules the router enforces around these ──────────────────────────────


def test_router_blocks_self_role_change() -> None:
    """Changing your own role would sidestep the seniority check entirely:
    grant yourself owner, then grant anything."""
    import inspect

    from suliko.api.v1 import users

    source = inspect.getsource(users.update_user)
    assert "row.id == session.user_id" in source
    assert "cannot change your own role" in source.lower()


def test_router_protects_the_last_owner() -> None:
    """A tenant with no active owner cannot be administered by anyone in it —
    it would need platform intervention to recover."""
    import inspect

    from suliko.api.v1 import users

    for handler in (users.update_user, users.delete_user):
        source = inspect.getsource(handler)
        assert "_active_owner_count" in source, (
            f"{handler.__name__} does not check for the last owner"
        )


def test_role_change_and_password_reset_revoke_sessions() -> None:
    """A demotion that only takes effect at next login is not a demotion —
    the user keeps their old permissions for up to the session lifetime."""
    import inspect

    from suliko.api.v1 import users

    assert "revoke_all_for_user" in inspect.getsource(users.update_user)
    assert "revoke_all_for_user" in inspect.getsource(users.reset_password)


def test_password_hash_is_never_returned() -> None:
    """UserOut must not carry the hash, even though the ORM row has it."""
    from suliko.api.v1.users import UserOut

    assert "password_hash" not in UserOut.model_fields
    assert "password" not in UserOut.model_fields
