"""The look of platform email, and a sweep over every message that uses it.

The layout makes the plain text and the HTML from one list of blocks, so what
needs checking is that the two say the same thing, that a person's words are
escaped in the HTML, and that the house rules for copy hold for every email.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html import unescape

from suliko.api.v1 import auth, translators, users
from suliko.core.mail_layout import (
    Bold,
    Button,
    Email,
    Facts,
    Link,
    Step,
    Steps,
    callout,
    compose,
    fine,
    p,
)
from suliko.domain import account_deletion as deletion
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Account, Role, User

LINK = "https://office.suliko.ge/en/accept-invite?token=rst_abc&x=1"


def _sample() -> Email:
    return compose(
        subject="Subject",
        heading="Heading",
        preheader="The grey line in the inbox",
        blocks=[
            p("Hello ", Bold("Nino"), ","),
            Facts([("Organisation", "Acme")]),
            Steps(
                [
                    Step(["Register"], Button("Register", "https://suliko.ge/r", kind="secondary")),
                    Step(["Accept"], Button("Accept", LINK)),
                ]
            ),
            callout("Careful: ", Link("this page", "https://suliko.ge/help")),
            fine("Fine print."),
        ],
        reason="You received this because of a test.",
    )


def test_both_versions_carry_every_link() -> None:
    message = _sample()

    for url in ("https://suliko.ge/r", "https://suliko.ge/help"):
        assert url in message.text
        assert url in message.html
    # An ampersand is escaped in the HTML attribute, and bare in the plain text.
    assert LINK in message.text
    assert LINK.replace("&", "&amp;") in message.html


def test_the_link_is_shown_as_text_as_well_as_behind_the_button() -> None:
    """So a person can check where it goes, and still use it when the button is
    stripped by a client."""
    html = _sample().html

    assert html.count(LINK.replace("&", "&amp;")) >= 2


def test_the_plain_text_numbers_the_steps_and_keeps_the_facts() -> None:
    text = _sample().text

    assert "1. Register" in text
    assert "2. Accept" in text
    assert "Organisation: Acme" in text
    assert text.index("1. Register") < text.index("2. Accept")
    assert text.rstrip().endswith("Suliko Office")


def test_the_preheader_and_reason_are_in_the_html() -> None:
    html = _sample().html

    assert "The grey line in the inbox" in html
    assert "You received this because of a test." in html


def test_what_a_person_typed_is_escaped_in_the_html() -> None:
    nasty = '<script>alert("x")</script> & "co"'
    message = compose(
        subject="S",
        heading=nasty,
        preheader=nasty,
        blocks=[
            p("Hello ", Bold(nasty)),
            Facts([(nasty, nasty)]),
            callout(Link(nasty, 'https://x.test/?a="b"&c=<d>')),
        ],
        reason=nasty,
    )

    assert "<script>" not in message.html
    assert "&lt;script&gt;" in message.html
    assert '"b"' not in message.html


def test_the_html_can_go_dark_and_narrow() -> None:
    html = _sample().html

    assert "prefers-color-scheme:dark" in html
    assert 'name="viewport"' in html
    assert 'name="color-scheme"' in html


def test_a_step_number_sits_in_a_cell_with_a_background_attribute() -> None:
    """Gmail drops the background of a sized div, which left a white digit on
    white (seen in a real inbox, 2026-10-15). A table cell with `bgcolor` stays."""
    html = _sample().html

    for number in ("1", "2"):
        assert 'valign="middle" bgcolor="#3b59f3"' in html
        assert f'text-align:center;">{number}</td>' in html
    assert '<div style="width:28px' not in html


def test_the_html_has_no_images() -> None:
    """Blocked by default in most clients, and a blocked image leaves a hole."""
    assert "<img" not in _sample().html


# ── Every email that uses it ────────────────────────────────────────────────


def _every_email() -> dict[str, Email]:
    account = Account(
        id=7, email="nino@acme.ge", full_name="Nino Beridze", phone="599123456", password_hash="x"
    )
    user = User(
        id=1,
        tenant_id=1,
        username="nino",
        email="nino@acme.ge",
        full_name="Nino Beridze",
        password_hash="x",
        role=Role.STAFF,
    )
    tenant = Tenant(id=1, slug="acme", display_name="Acme", status=TenantStatus.ACTIVE)
    request = deletion.AccountDeletionRequest(
        id=12,
        account_id=7,
        created_at=datetime.now(UTC),
        purge_after=datetime.now(UTC) + timedelta(days=30),
    )
    linked = users.SulikoAccountOut(status="linked", matched_display_name="Nino")
    pending = users.SulikoAccountOut(status="pending", matched_display_name=None)
    return {
        "reset": auth._reset_email(account, LINK, 60),
        "reset on suliko.ge": auth._suliko_password_email(account, "https://suliko.ge/login"),
        "confirm": auth._verification_email(user, tenant, LINK, 72),
        "personal enabled": auth._personal_enabled_email(account, LINK),
        "invite, new to suliko": users._invite_email(
            user, "Acme", LINK, "Boss", 7, existing_account=False,
            suliko_account=pending, needs_registration=True,
        ),
        "invite, existing": users._invite_email(
            user, "Acme", LINK, "Boss", 7, existing_account=True, suliko_account=linked
        ),
        "invite, set password": users._invite_email(
            user, "Acme", LINK, "Boss", 7, existing_account=False, suliko_account=pending
        ),
        "translator, pending": translators._translator_invite_email(
            "Giorgi", "Acme", "g@example.com", linked=False, matched_display_name=None
        ),
        "translator, linked": translators._translator_invite_email(
            "Giorgi", "Acme", "g@example.com", linked=True, matched_display_name="Giorgi"
        ),
        "deletion, team": deletion.team_email(
            account, request, organisation="Acme", organisation_slug="acme"
        ),
        "deletion, received": deletion.person_email(account),
        "deletion, confirmed": deletion.approved_email(account, request),
        "deletion, cancelled": deletion.rejected_email(account),
        "deletion, done": deletion.done_email("Nino"),
    }  # fmt: skip


def test_no_email_uses_an_em_dash() -> None:
    """House rule for every word the product shows: no em dashes."""
    for name, email in _every_email().items():
        for version in (email.subject, email.text, email.html):
            assert "—" not in version, name


def _words(markup: str) -> set[str]:
    visible = unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<style.*?</style>", "", markup)))
    return {word.strip(".'-") for word in re.findall(r"[\w@.+'-]{4,}", visible)}


def test_the_plain_text_says_nothing_the_html_does_not() -> None:
    """Every word of the plain text is in the HTML. The reverse is not true (the
    HTML has the preheader and the wordmark), which is fine."""
    for name, email in _every_email().items():
        missing = _words(email.text) - _words(email.html)
        assert not missing, (name, missing)


def test_every_email_says_why_it_was_sent() -> None:
    for name, email in _every_email().items():
        assert "You received this because" in email.text, name
        assert "You received this because" in email.html, name


def test_every_email_has_one_obvious_thing_to_do_or_nothing_to_do() -> None:
    """A button, or no button at all: never two primaries."""
    for name, email in _every_email().items():
        primaries = email.html.count("color:#ffffff;text-decoration:none")
        assert primaries <= 2, name
