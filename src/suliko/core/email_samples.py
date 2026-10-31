"""Every platform email, built from fixed sample data.

Two uses. `python -m suliko.cli preview-emails --out <dir>` writes each one as
an HTML and a text file (and an index) to open in a browser, so a change to
the wording or the layout is seen before it is sent. And `tests/test_email_snapshots.py`
compares them with the last approved version, so a change to an email is never
an accident: it fails until the snapshot is updated on purpose.

Nothing here touches a database or sends anything. The data is fixed, dates
included, so the output is the same every run.

The order-files email is not here: it is a person sending files to a person,
built elsewhere (`domain/order_mail_message.py`) and kept plain on purpose.
"""

# Sample data, not credentials: the "password hashes" below are placeholders.
# ruff: noqa: S106

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

from suliko.core.mail_layout import Email

_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
_LINK = "https://office.suliko.ge/ka/accept-invite?token=rst_Vq3m8Zk1b0HfX2nTt9sYw7dLpA4cRjEu6"
_RESET = "https://office.suliko.ge/ka/reset-password?token=rst_Vq3m8Zk1b0HfX2nTt9sYw7dLpA4cRjEu6"


def sample_emails() -> dict[str, Email]:
    """Name -> email, in the order they are listed."""
    # Imported here: the routers pull in the whole application.
    from suliko.api.v1 import auth, translators, users
    from suliko.domain import account_deletion as deletion
    from suliko.models.tenant import Tenant, TenantStatus
    from suliko.models.user import Account, AccountDeletionRequest, Role, User

    account = Account(
        id=7, email="nino@acme.ge", full_name="Nino Beridze", phone="599123456", password_hash="x"
    )
    user = User(
        id=1,
        tenant_id=1,
        username="nino",
        email="nino+office@gmail.com",
        full_name="Nino Beridze",
        password_hash="x",
        role=Role.STAFF,
    )
    tenant = Tenant(id=1, slug="acme", display_name="Acme Translations", status=TenantStatus.ACTIVE)
    request = AccountDeletionRequest(
        id=12, account_id=7, created_at=_NOW, purge_after=_NOW + timedelta(days=30)
    )
    linked = users.SulikoAccountOut(status="linked", matched_display_name="Nino Beridze")
    pending = users.SulikoAccountOut(status="pending", matched_display_name=None)
    inviter = "Mikheil Katenishvili"

    return {
        "01-reset-password": auth._reset_email(account, _RESET, 60),
        "02-reset-on-suliko-ge": auth._suliko_password_email(account, "https://suliko.ge/sign-in"),
        "03-confirm-email": auth._verification_email(user, tenant, _RESET, 72),
        "04-personal-enabled": auth._personal_enabled_email(
            account, "https://office.suliko.ge/ka/login"
        ),
        "05-invite-new-to-suliko": users._invite_email(
            user,
            "Acme Translations",
            _LINK,
            inviter,
            7,
            existing_account=False,
            suliko_account=pending,
            needs_registration=True,
        ),
        "06-invite-existing-account": users._invite_email(
            user,
            "Acme Translations",
            _LINK,
            inviter,
            7,
            existing_account=True,
            suliko_account=linked,
        ),
        "07-invite-set-password": users._invite_email(
            user,
            "Acme Translations",
            _RESET,
            inviter,
            7,
            existing_account=False,
            suliko_account=pending,
        ),
        "08-translator-invite-pending": translators._translator_invite_email(
            "Giorgi Kapanadze",
            "Acme Translations",
            "giorgi@example.com",
            linked=False,
            matched_display_name=None,
        ),
        "09-translator-invite-linked": translators._translator_invite_email(
            "Giorgi Kapanadze",
            "Acme Translations",
            "giorgi@example.com",
            linked=True,
            matched_display_name="Giorgi K.",
        ),
        "10-deletion-team": deletion.team_email(
            account, request, organisation="Acme Translations", organisation_slug="acme"
        ),
        "11-deletion-received": deletion.person_email(account),
        "12-deletion-confirmed": deletion.approved_email(account, request),
        "13-deletion-cancelled": deletion.rejected_email(account),
        "14-deletion-done": deletion.done_email("Nino Beridze"),
    }


def write_previews(directory: Path) -> list[str]:
    """Write every email as `<name>.html` and `<name>.txt`, plus `index.html`.

    Returns the lines to print: one per email, with its subject.
    """
    directory.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    items: list[str] = []
    for name, email in sample_emails().items():
        (directory / f"{name}.html").write_text(email.html, encoding="utf-8")
        (directory / f"{name}.txt").write_text(email.text, encoding="utf-8")
        lines.append(f"{name} | {email.subject}")
        items.append(f'<li><a href="{name}.html">{name}</a>: {escape(email.subject)}</li>')
    (directory / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Emails</title><ul>' + "".join(items) + "</ul>",
        encoding="utf-8",
    )
    return lines
