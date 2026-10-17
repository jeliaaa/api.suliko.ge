"""First-run tips: which ones exist, who gets them, and what they have seen.

The screens decide WHERE a tip is pinned; this module only knows the ids, so
that the API can refuse a made-up one and the app and the API agree on the
list. An id is `<place>-<thing>`, lower-case, hyphenated.

Seen-state is per account (`accounts.tips_seen`, a list of ids). Whether tips
are on at all is a function of the account's age (`tips_accounts_since` in the
settings): an account that existed before the release never sees them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from suliko.models.user import Account

#: In the order the tour walks them.
TIP_IDS: tuple[str, ...] = (
    "nav-translations",
    "nav-clients",
    "nav-translators",
    "nav-calculator",
    "nav-finances",
    "translations-new",
    "order-translate-send",
    "users-invite",
)


def tips_enabled(
    account: Account | None,
    *,
    since: datetime,
    onboarding_required: bool,
    impersonated: bool,
) -> bool:
    """Whether to show tips: a new account, past the plan choice, not someone
    acting as them (a superuser must not mark the person's tips seen)."""
    if account is None or onboarding_required or impersonated:
        return False
    created = account.created_at
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    # A bare date in TIPS_ACCOUNTS_SINCE (2026-10-20) parses as a naive datetime;
    # comparing that with an aware one raises, so read it as UTC too.
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return created >= since


def seen_tips(account: Account | None) -> list[str]:
    """The stored ids that are still real tips, in tour order."""
    stored = set(account.tips_seen or []) if account is not None else set()
    return [tip for tip in TIP_IDS if tip in stored]


def apply_update(account: Account, *, seen: list[str], reset: bool) -> list[str]:
    """Mark `seen` as seen, or clear everything when `reset`. Returns the new list."""
    wanted = set() if reset else set(account.tips_seen or []) | set(seen)
    account.tips_seen = [tip for tip in TIP_IDS if tip in wanted]
    return list(account.tips_seen)
