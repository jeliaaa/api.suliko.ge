"""A platform email never changes by accident.

Each email is built from fixed sample data (`core/email_samples.py`) and
compared with the last approved version: subject and text in full, the HTML by
hash (it is 10 to 25 KB each). A change to the wording or the layout fails here
until the snapshot is updated on purpose, which is the moment to look at it:

    python -m suliko.cli preview-emails --out previews     # open previews/index.html
    UPDATE_EMAIL_SNAPSHOTS=1 python -m pytest tests/test_email_snapshots.py
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from suliko.core.email_samples import sample_emails, write_previews

SNAPSHOT = Path(__file__).parent / "fixtures" / "email_snapshots.json"


def _digest() -> dict[str, dict[str, str]]:
    return {
        name: {
            "subject": email.subject,
            "text": email.text,
            "html_sha256": hashlib.sha256(email.html.encode("utf-8")).hexdigest(),
        }
        for name, email in sample_emails().items()
    }


def test_the_emails_are_the_approved_ones() -> None:
    current = _digest()
    if os.environ.get("UPDATE_EMAIL_SNAPSHOTS"):
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        pytest.skip("snapshots updated")

    approved = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert sorted(current) == sorted(approved), "an email was added or removed"
    changed = [name for name in current if current[name] != approved[name]]
    assert not changed, (
        f"changed since approved: {', '.join(changed)}. If that is intended: "
        "UPDATE_EMAIL_SNAPSHOTS=1 python -m pytest tests/test_email_snapshots.py"
    )


def test_building_them_twice_gives_the_same_result() -> None:
    """Nothing in a sample may depend on the clock or on chance."""
    assert _digest() == _digest()


def test_there_are_fourteen_and_none_uses_an_em_dash() -> None:
    emails = sample_emails()
    assert len(emails) == 14
    for name, email in emails.items():
        assert "—" not in email.text + email.html + email.subject, name


def test_the_preview_command_writes_every_email_and_an_index(tmp_path: Path) -> None:
    lines = write_previews(tmp_path)

    assert len(lines) == 14
    for name in sample_emails():
        assert (tmp_path / f"{name}.html").read_text(encoding="utf-8").startswith("<!doctype html")
        assert (tmp_path / f"{name}.txt").read_text(encoding="utf-8").strip()
    assert "01-reset-password" in (tmp_path / "index.html").read_text(encoding="utf-8")
