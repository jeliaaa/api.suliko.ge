"""The frontend's message catalogues stay complete in both languages.

The app defaults to Georgian. A key present in `en.json` and missing from
`ka.json` renders as the raw key path on a Georgian screen; a status with no
label renders a grey "Unknown"; an ICU placeholder spelled differently in the
two files throws at render time in one language only. All three are cheap to
catch here and expensive to find in production.

Like `test_parity.py`, these skip when the frontend is not checked out next to
this repo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from suliko.domain.statuses import STATUS_DEFINITIONS

MESSAGES = Path(__file__).resolve().parents[2] / "app.suliko.ge" / "messages"

requires_messages = pytest.mark.skipif(
    not (MESSAGES / "en.json").exists(), reason="app.suliko.ge not checked out alongside"
)


def _flatten(tree: dict[str, Any], prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in tree.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, path))
        else:
            out[path] = value
    return out


def _load(lang: str) -> dict[str, str]:
    return _flatten(json.loads((MESSAGES / f"{lang}.json").read_text(encoding="utf-8")))


def _placeholders(message: str) -> set[str]:
    """Top-level ICU argument names — `{name}`, `{count, plural, …}`."""
    names: set[str] = set()
    depth = 0
    for index, char in enumerate(message):
        if char == "{":
            if depth == 0:
                match = re.match(r"\{\s*([A-Za-z_]\w*)\s*[,}]", message[index:])
                if match:
                    names.add(match.group(1))
            depth += 1
        elif char == "}":
            depth -= 1
    return names


@requires_messages
def test_both_languages_have_the_same_keys() -> None:
    en, ka = _load("en"), _load("ka")
    assert sorted(set(en) - set(ka)) == [], "missing from ka.json"
    assert sorted(set(ka) - set(en)) == [], "missing from en.json"


@requires_messages
def test_no_message_is_empty() -> None:
    for lang in ("en", "ka"):
        empty = [key for key, value in _load(lang).items() if not str(value).strip()]
        assert empty == [], f"empty messages in {lang}.json"


@requires_messages
def test_placeholders_match_between_languages() -> None:
    en, ka = _load("en"), _load("ka")
    mismatched = {
        key: (sorted(_placeholders(en[key])), sorted(_placeholders(ka[key])))
        for key in en.keys() & ka.keys()
        if _placeholders(en[key]) != _placeholders(ka[key])
    }
    assert mismatched == {}


@requires_messages
def test_every_status_has_a_label_in_both_languages() -> None:
    # `statusMessageKey` in statuses.ts: lower-case, spaces to underscores.
    for lang in ("en", "ka"):
        messages = _load(lang)
        missing = [
            status
            for status in STATUS_DEFINITIONS
            if f"statuses.{status.strip().lower().replace(' ', '_')}" not in messages
        ]
        assert missing == [], f"statuses without a label in {lang}.json"
